"""Tests for few_shot_miner — moto S3 + DDB; synthetic stream records."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import boto3
import few_shot_miner.handler as miner
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from few_shot_miner.handler import ddb, handler, s3
from moto import mock_aws

ARTIFACTS = "test-artifacts"
RUNS = "test-runs"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="few_shot_miner-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
            get_remaining_time_in_millis=lambda: 30_000,
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars + create runs table + artifacts bucket."""
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    s3.cache_clear()
    ddb.cache_clear()
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=ARTIFACTS)
        boto3.client("dynamodb").create_table(
            TableName=RUNS,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    s3.cache_clear()
    ddb.cache_clear()


def seed_run_events(run_id: str, *, with_tasks: tuple[str, ...] = ("T-001",)) -> None:
    """Insert a fake run timeline so the miner has events to mine."""
    items = [
        {
            "pk": f"RUN#{run_id}",
            "sk": "EVENT#01#req",
            "type": "REQUEST.RECEIVED",
            "envelope": {
                "type": "REQUEST.RECEIVED",
                "run_id": run_id,
                "payload": {
                    "project_slug": "demo",
                    "intent": "Add /healthz endpoint",
                    "requestor": "alice",
                },
            },
        },
        {
            "pk": f"RUN#{run_id}",
            "sk": "EVENT#02#spec",
            "type": "SPEC.READY",
            "envelope": {
                "type": "SPEC.READY",
                "run_id": run_id,
                "payload": {
                    "project_slug": "demo",
                    "spec_slug": "add-healthz",
                    "spec_s3_prefix": "specs/add-healthz/",
                    "task_count": len(with_tasks),
                    "task_ids": list(with_tasks),
                },
            },
        },
    ]
    for ix, task_id in enumerate(with_tasks, start=3):
        items.append(
            {
                "pk": f"RUN#{run_id}",
                "sk": f"EVENT#{ix:02d}#tready-{task_id}",
                "type": "TASK.READY",
                "envelope": {
                    "type": "TASK.READY",
                    "run_id": run_id,
                    "payload": {
                        "project_slug": "demo",
                        "spec_slug": "add-healthz",
                        "spec_s3_prefix": "specs/add-healthz/",
                        "task_id": task_id,
                        "pr_url": f"https://github.com/x/y/pull/{task_id}",
                        "diff_summary": "diff: ...",
                    },
                },
            },
        )
        items.append(
            {
                "pk": f"RUN#{run_id}",
                "sk": f"EVENT#{ix:02d}#tapproved-{task_id}",
                "type": "TASK.APPROVED",
                "envelope": {
                    "type": "TASK.APPROVED",
                    "run_id": run_id,
                    "payload": {
                        "project_slug": "demo",
                        "spec_slug": "add-healthz",
                        "task_id": task_id,
                        "reviewer": "alice",
                        "pr_url": f"https://github.com/x/y/pull/{task_id}",
                    },
                },
            },
        )
    for item in items:
        pk = cast("str", item["pk"])
        sk = cast("str", item["sk"])
        item_type = cast("str", item["type"])
        ddb().put_item(
            TableName=RUNS,
            Item={
                "pk": {"S": pk},
                "sk": {"S": sk},
                "type": {"S": item_type},
                "envelope": {"S": json.dumps(item["envelope"])},
            },
        )


def state_stream_record(*, run_id: str, status: str, rejections: str) -> dict[str, Any]:
    """Build a synthetic DDB stream record for a STATE row update."""
    return {
        "eventName": "MODIFY",
        "dynamodb": {
            "NewImage": {
                "pk": {"S": f"RUN#{run_id}"},
                "sk": {"S": "STATE"},
                "status": {"S": status},
                "spec_slug": {"S": "add-healthz"},
                "project_slug": {"S": "demo"},
                "total_rejections": {"N": rejections},
            },
        },
    }


def test_clean_run_emits_intent_and_task_examples() -> None:
    seed_run_events("run-1", with_tasks=("T-001", "T-002"))
    out = handler(
        {"Records": [state_stream_record(run_id="run-1", status="RUN.COMPLETED", rejections="0")]},
        ctx(),
    )
    assert out == {"batchItemFailures": []}
    keys = [
        item["Key"]
        for item in s3()
        .list_objects_v2(Bucket=ARTIFACTS, Prefix="evals/few-shots/")
        .get("Contents", [])
    ]
    assert any(k.startswith("evals/few-shots/intent_to_spec/") for k in keys)
    task_keys = [k for k in keys if k.startswith("evals/few-shots/task_to_diff/")]
    assert len(task_keys) == 2


def test_rejected_run_is_skipped() -> None:
    seed_run_events("run-2")
    out = handler(
        {"Records": [state_stream_record(run_id="run-2", status="RUN.COMPLETED", rejections="3")]},
        ctx(),
    )
    assert out == {"batchItemFailures": []}
    keys = s3().list_objects_v2(Bucket=ARTIFACTS, Prefix="evals/few-shots/").get("Contents", [])
    assert not keys


def test_in_progress_run_is_skipped() -> None:
    seed_run_events("run-3")
    out = handler(
        {"Records": [state_stream_record(run_id="run-3", status="SPEC.READY", rejections="0")]},
        ctx(),
    )
    assert out == {"batchItemFailures": []}


def test_event_row_update_is_skipped() -> None:
    seed_run_events("run-4")
    record = {
        "eventName": "MODIFY",
        "dynamodb": {
            "NewImage": {
                "pk": {"S": "RUN#run-4"},
                "sk": {"S": "EVENT#99#x"},
                "status": {"S": "RUN.COMPLETED"},
                "total_rejections": {"N": "0"},
            },
        },
    }
    out = handler({"Records": [record]}, ctx())
    assert out == {"batchItemFailures": []}


def test_remove_event_skipped() -> None:
    out = handler({"Records": [{"eventName": "REMOVE"}]}, ctx())
    assert out == {"batchItemFailures": []}


def test_intent_payload_carries_intent_string() -> None:
    seed_run_events("run-5")
    handler(
        {"Records": [state_stream_record(run_id="run-5", status="RUN.COMPLETED", rejections="0")]},
        ctx(),
    )
    obj = s3().get_object(
        Bucket=ARTIFACTS,
        Key=next(
            item["Key"]
            for item in s3()
            .list_objects_v2(
                Bucket=ARTIFACTS,
                Prefix="evals/few-shots/intent_to_spec/",
            )
            .get("Contents", [])
        ),
    )
    record = json.loads(obj["Body"].read())
    assert record["input"]["intent"] == "Add /healthz endpoint"
    assert record["output"]["spec_slug"] == "add-healthz"


def test_task_payload_carries_diff_summary() -> None:
    seed_run_events("run-6", with_tasks=("T-007",))
    handler(
        {"Records": [state_stream_record(run_id="run-6", status="RUN.COMPLETED", rejections="0")]},
        ctx(),
    )
    obj = s3().get_object(
        Bucket=ARTIFACTS,
        Key=next(
            item["Key"]
            for item in s3()
            .list_objects_v2(
                Bucket=ARTIFACTS,
                Prefix="evals/few-shots/task_to_diff/",
            )
            .get("Contents", [])
        ),
    )
    record = json.loads(obj["Body"].read())
    assert record["task_id"] == "T-007"
    assert record["output"]["diff_summary"] == "diff: ..."


def test_unannounced_task_excluded() -> None:
    """A TASK.READY without a matching TASK.APPROVED is skipped."""
    seed_run_events("run-7")
    # remove the approval
    ddb().delete_item(
        TableName=RUNS,
        Key={"pk": {"S": "RUN#run-7"}, "sk": {"S": "EVENT#03#tapproved-T-001"}},
    )
    out = handler(
        {"Records": [state_stream_record(run_id="run-7", status="RUN.COMPLETED", rejections="0")]},
        ctx(),
    )
    assert out == {"batchItemFailures": []}  # the run still mines — just the task is skipped
    task_keys = [
        item["Key"]
        for item in s3()
        .list_objects_v2(
            Bucket=ARTIFACTS,
            Prefix="evals/few-shots/task_to_diff/",
        )
        .get("Contents", [])
    ]
    assert not task_keys


def test_partial_failure_isolates_poison_record() -> None:
    """A failing record reports its sequence number; siblings still process."""
    seed_run_events("run-ok", with_tasks=("T-001",))
    good = {
        "eventID": "good",
        "eventName": "MODIFY",
        "eventSource": "aws:dynamodb",
        "eventVersion": "1.1",
        "awsRegion": "us-east-1",
        "dynamodb": {
            "SequenceNumber": "100",
            "Keys": {"pk": {"S": "RUN#run-ok"}, "sk": {"S": "STATE"}},
            "NewImage": {
                "pk": {"S": "RUN#run-ok"},
                "sk": {"S": "STATE"},
                "status": {"S": "RUN.COMPLETED"},
                "spec_slug": {"S": "add-healthz"},
                "project_slug": {"S": "demo"},
                "total_rejections": {"N": "0"},
            },
        },
    }
    poison = {
        "eventID": "poison",
        "eventName": "MODIFY",
        "eventSource": "aws:dynamodb",
        "eventVersion": "1.1",
        "awsRegion": "us-east-1",
        "dynamodb": {
            "SequenceNumber": "200",
            "Keys": {"pk": {"S": "RUN#poison"}, "sk": {"S": "STATE"}},
            "NewImage": {
                "pk": {"S": "RUN#poison"},
                "sk": {"S": "STATE"},
                "status": {"S": "RUN.COMPLETED"},
                "project_slug": {"S": "demo"},
                # invalid total_rejections type triggers the int() in mine_record;
                # a non-numeric string passes the isdigit guard but to force a
                # genuine failure we'll patch fetch_events.
                "total_rejections": {"N": "0"},
            },
        },
    }
    original_fetch = miner.fetch_events

    def fetch_or_raise(run_id: str) -> list[dict[str, Any]]:
        if run_id == "poison":
            msg = "boom"
            raise RuntimeError(msg)
        return original_fetch(run_id)

    setattr(miner, "fetch_events", fetch_or_raise)  # noqa: B010
    try:
        out = handler({"Records": [good, poison]}, ctx())
    finally:
        setattr(miner, "fetch_events", original_fetch)  # noqa: B010
    assert out["batchItemFailures"] == [{"itemIdentifier": "200"}]
    contents = (
        s3()
        .list_objects_v2(Bucket=ARTIFACTS, Prefix="evals/few-shots/intent_to_spec/")
        .get("Contents", [])
    )
    assert contents, "the good record's example should have been written"
