"""Tests for eval_runner — moto S3 + DDB + CloudWatch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from eval_runner.handler import (
    PassCriteria,
    check_pass_criteria,
    cw,
    ddb,
    handler,
    s3,
    state_to_metrics,
)
from moto import mock_aws

ARTIFACTS = "test-artifacts"
RUNS = "test-runs"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="eval_runner-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars + create runs table + artifacts bucket under moto."""
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    s3.cache_clear()
    ddb.cache_clear()
    cw.cache_clear()
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
    cw.cache_clear()


def seed_state(run_id: str, **attrs: str) -> None:
    """Insert a fake STATE row for evaluator tests."""
    item: dict[str, dict[str, Any]] = {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": "STATE"},
        "status": {"S": "RUN.COMPLETED"},
        "project_slug": {"S": "demo"},
    }
    for k, v in attrs.items():
        item[k] = {"N": v}
    ddb().put_item(TableName=RUNS, Item=item)


def case() -> dict[str, Any]:
    """A minimal valid case for evaluate_result tests."""
    return {
        "slug": "small-feature-add",
        "project_slug": "echo",
        "intent": "Add a /version route",
        "pass_criteria": {
            "min_task_count": 2,
            "max_task_count": 4,
            "max_cost_usd": 2.0,
            "max_duration_minutes": 30,
            "allow_rejections": False,
        },
    }


def test_load_cases_with_override_returns_validated_list() -> None:
    out = handler({"op": "load_cases", "override_cases": [case()]}, ctx())
    assert out["ok"] is True
    assert len(out["cases"]) == 1
    assert out["cases"][0]["slug"] == "small-feature-add"


def test_load_cases_from_s3() -> None:
    s3().put_object(
        Bucket=ARTIFACTS,
        Key="evals/cases.yaml",
        Body=b"cases:\n"
        b"  - slug: x-test\n"
        b"    project_slug: demo\n"
        b"    intent: do a thing\n"
        b"    pass_criteria:\n"
        b"      min_task_count: 1\n"
        b"      max_task_count: 3\n"
        b"      max_cost_usd: 1.0\n"
        b"      max_duration_minutes: 30\n"
        b"      allow_rejections: false\n",
    )
    out = handler({"op": "load_cases"}, ctx())
    assert out["ok"] is True
    assert out["cases"][0]["slug"] == "x-test"


def test_evaluate_result_passes_within_thresholds() -> None:
    seed_state(
        "run-1",
        tasks_completed="3",
        total_cost_usd="1.50",
        total_duration_ms="600000",  # 10 min
        total_rejections="0",
    )
    out = handler({"op": "evaluate_result", "case": case(), "run_id": "run-1"}, ctx())
    assert out["passed"] is True
    assert out["failures"] == []


def test_evaluate_result_fails_on_cost_overrun() -> None:
    seed_state(
        "run-2",
        tasks_completed="3",
        total_cost_usd="3.50",  # > max 2.0
        total_duration_ms="600000",
        total_rejections="0",
    )
    out = handler({"op": "evaluate_result", "case": case(), "run_id": "run-2"}, ctx())
    assert out["passed"] is False
    assert any("cost" in f for f in out["failures"])


def test_evaluate_result_fails_on_task_count_overrun() -> None:
    seed_state(
        "run-3",
        tasks_completed="6",  # > max 4
        total_cost_usd="1.50",
        total_duration_ms="600000",
        total_rejections="0",
    )
    out = handler({"op": "evaluate_result", "case": case(), "run_id": "run-3"}, ctx())
    assert out["passed"] is False
    assert any("task_count" in f and "max" in f for f in out["failures"])


def test_evaluate_result_unexpected_rejections_when_disallowed() -> None:
    seed_state(
        "run-4",
        tasks_completed="3",
        total_cost_usd="1.0",
        total_duration_ms="600000",
        total_rejections="2",
    )
    out = handler({"op": "evaluate_result", "case": case(), "run_id": "run-4"}, ctx())
    assert out["passed"] is False
    assert any("rejection" in f.lower() for f in out["failures"])


def test_evaluate_result_run_not_found() -> None:
    out = handler({"op": "evaluate_result", "case": case(), "run_id": "missing"}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "run_not_found"


def test_record_result_persists_to_s3() -> None:
    out = handler(
        {
            "op": "record_result",
            "case_slug": "small-feature-add",
            "run_id": "run-1",
            "passed": True,
            "failures": [],
            "metrics": {"total_cost_usd": 1.5, "tasks_completed": 3},
        },
        ctx(),
    )
    assert out["ok"] is True
    obj = s3().get_object(Bucket=ARTIFACTS, Key=out["key"])
    record = json.loads(obj["Body"].read())
    assert record["case_slug"] == "small-feature-add"
    assert record["passed"] is True


def test_aggregate_results_emits_pass_rate() -> None:
    out = handler(
        {
            "op": "aggregate_results",
            "results": [
                {"passed": True},
                {"passed": True},
                {"passed": False},
            ],
        },
        ctx(),
    )
    assert out["passed"] == 2
    assert out["total"] == 3
    assert out["pass_rate"] == pytest.approx(2 / 3)


def test_aggregate_results_zero_results() -> None:
    out = handler({"op": "aggregate_results", "results": []}, ctx())
    assert out["pass_rate"] == 0.0
    assert out["total"] == 0


def test_unknown_op_returns_error() -> None:
    out = handler({"op": "frobnicate"}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_op"


def test_invalid_event_shape() -> None:
    out = handler(cast("dict[str, Any]", []), ctx())
    assert out["ok"] is False


def test_check_pass_criteria_unit() -> None:
    crit = PassCriteria(
        min_task_count=2,
        max_task_count=4,
        max_cost_usd=2.0,
        max_duration_minutes=30,
        allow_rejections=False,
    )
    metrics = {
        "tasks_completed": 3.0,
        "task_count": 3.0,
        "total_cost_usd": 1.5,
        "total_duration_ms": 600000.0,
        "total_rejections": 0.0,
    }
    assert check_pass_criteria(metrics, crit) == []


def test_state_to_metrics_pulls_numbers() -> None:
    item = {
        "tasks_completed": {"N": "3"},
        "total_cost_usd": {"N": "1.50"},
        "total_duration_ms": {"N": "600000"},
        "total_rejections": {"N": "0"},
    }
    out = state_to_metrics(item)
    assert out["tasks_completed"] == 3.0
    assert out["total_cost_usd"] == 1.5
