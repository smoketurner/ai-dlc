"""Tests for GET /runs/{run_id} — server-rendered GitHub-link surfaces."""

from __future__ import annotations

import json
from collections.abc import Iterator

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from dashboard.app import app
from dashboard.deps import ddb, s3, settings

RUNS = "test-runs"
ARTIFACTS = "test-artifacts"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Spin up moto-backed DDB + S3 and disable Cognito auth."""
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS)
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("AIDLC_AUTH", "disabled")
    settings.cache_clear()
    ddb.cache_clear()
    s3.cache_clear()
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=RUNS,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=ARTIFACTS)
        yield
    settings.cache_clear()
    ddb.cache_clear()
    s3.cache_clear()


def seed_state(run_id: str, **extra: object) -> None:
    """Write a STATE row with optional GitHub-linkage attributes."""
    current_state = str(extra.pop("current_state", "received"))
    item: dict[str, dict[str, str]] = {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": "STATE"},
        "current_state": {"S": current_state},
        "status": {"S": "RUNNING"},
        "project_slug": {"S": "acme"},
    }
    for key in ("target_repo", "source_issue_url", "issue_title", "pr_url"):
        value = extra.get(key)
        if value is not None:
            item[key] = {"S": str(value)}
    issue_number = extra.get("issue_number")
    if issue_number is not None:
        item["issue_number"] = {"N": str(issue_number)}
    ddb().put_item(TableName=RUNS, Item=item)


def seed_event(run_id: str, *, event_id: str, event_type: str, payload: dict) -> None:
    envelope = json.dumps(
        {"event_id": event_id, "type": event_type, "timestamp": "t", "payload": payload}
    )
    ddb().put_item(
        TableName=RUNS,
        Item={
            "pk": {"S": f"RUN#{run_id}"},
            "sk": {"S": f"EVENT#{event_id}"},
            "type": {"S": event_type},
            "envelope": {"S": envelope},
        },
    )


def seed_task(run_id: str, task_id: str, *, status: str, pr_url: str, at: str) -> None:
    ddb().put_item(
        TableName=RUNS,
        Item={
            "pk": {"S": f"RUN#{run_id}"},
            "sk": {"S": f"TASK#{task_id}"},
            "status": {"S": status},
            "pr_url": {"S": pr_url},
            "last_event_at": {"S": at},
        },
    )


def test_renders_issue_and_repo_links_in_header() -> None:
    seed_state(
        "r1",
        target_repo="smoketurner/ai-dlc",
        source_issue_url="https://github.com/smoketurner/ai-dlc/issues/42",
        issue_number=42,
        issue_title="add login",
    )
    seed_event("r1", event_id="0001", event_type="REQUEST.RECEIVED", payload={})

    with TestClient(app) as client:
        resp = client.get("/runs/r1")

    assert resp.status_code == 200
    body = resp.text
    assert "https://github.com/smoketurner/ai-dlc" in body
    assert "smoketurner/ai-dlc" in body
    assert "https://github.com/smoketurner/ai-dlc/issues/42" in body
    assert "#42" in body
    assert "add login" in body


def test_renders_pull_requests_section_with_spec_and_task_prs() -> None:
    seed_state("r2", pr_url="https://github.com/o/r/pull/1", current_state="tasks_in_progress")
    seed_event("r2", event_id="0001", event_type="SPEC.READY", payload={})
    seed_task(
        "r2",
        "t1",
        status="approved",
        pr_url="https://github.com/o/r/pull/2",
        at="2026-05-11T00:00:00Z",
    )
    seed_task(
        "r2",
        "t2",
        status="rejected",
        pr_url="https://github.com/o/r/pull/3",
        at="2026-05-11T01:00:00Z",
    )

    with TestClient(app) as client:
        resp = client.get("/runs/r2")

    assert resp.status_code == 200
    body = resp.text
    assert "Pull requests" in body
    assert "https://github.com/o/r/pull/1" in body
    assert "https://github.com/o/r/pull/2" in body
    assert "https://github.com/o/r/pull/3" in body
    # Status pills use literal classes — both terminal states are present.
    assert "bg-emerald-100" in body
    assert "bg-rose-100" in body


def test_omits_pull_requests_section_when_no_prs() -> None:
    seed_state("r3")
    seed_event("r3", event_id="0001", event_type="ISSUE.TRIAGED", payload={"action": "decline"})

    with TestClient(app) as client:
        resp = client.get("/runs/r3")

    assert resp.status_code == 200
    assert "Pull requests" not in resp.text


def test_renders_link_pill_on_event_with_pr_url() -> None:
    seed_state("r4")
    seed_event(
        "r4",
        event_id="0001",
        event_type="TASK.READY",
        payload={"task_id": "t1", "pr_url": "https://github.com/o/r/pull/9"},
    )

    with TestClient(app) as client:
        resp = client.get("/runs/r4")

    assert resp.status_code == 200
    body = resp.text
    # The link pill appears in the timeline row.
    assert 'href="https://github.com/o/r/pull/9"' in body
    assert ">PR<" in body


def test_events_json_carries_links_field() -> None:
    seed_state("r5")
    seed_event(
        "r5",
        event_id="0001",
        event_type="ISSUE.TRIAGED",
        payload={"issue_url": "https://github.com/o/r/issues/3", "action": "proceed"},
    )

    with TestClient(app) as client:
        resp = client.get("/v1/runs/r5/events")

    assert resp.status_code == 200
    events = resp.json()["events"]
    assert events[0]["links"] == [{"label": "issue", "url": "https://github.com/o/r/issues/3"}]


def test_dedupes_links_when_pr_url_equals_html_url() -> None:
    seed_state("r6")
    url = "https://github.com/o/r/pull/4"
    seed_event(
        "r6",
        event_id="0001",
        event_type="TASK.READY",
        payload={"pr_url": url, "html_url": url},
    )

    with TestClient(app) as client:
        resp = client.get("/v1/runs/r6/events")

    assert resp.json()["events"][0]["links"] == [{"label": "PR", "url": url}]
