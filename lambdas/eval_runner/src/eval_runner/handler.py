"""Eval-runner Lambda — four ops dispatched on ``op`` from Step Functions.

The eval state machine calls this Lambda at four stages:

  * ``load_cases``       — read ``evals/cases.yaml`` from S3 and return the
    list of cases (or an explicit ``override`` list passed in by the GH
    Actions trigger).
  * ``evaluate_result``  — pull the SDLC pipeline run's STATE row from
    DynamoDB and compare totals against the case's pass criteria.
  * ``record_result``    — write the evaluated result to
    ``evals/results/{date}/{case_slug}.json`` and emit per-case
    CloudWatch metrics.
  * ``aggregate_results``— compute the suite-wide pass rate and emit the
    ``AIDLC/Evals/PassRate`` metric the drift alarm watches.

The Lambda is intentionally read-only against pipeline tables and never
mutates SDLC state — it only persists eval-result records.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import boto3
import yaml
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch.client import CloudWatchClient
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="eval_runner")
tracer = Tracer(service="eval_runner")
metrics = Metrics(namespace="ai-dlc", service="eval_runner")


class BaseOp(BaseModel):
    """Strict frozen base for every op input."""

    model_config = ConfigDict(extra="forbid", strict=True)


class LoadCasesInput(BaseOp):
    """Read the case index. Optional override skips the S3 read.

    ``tier_filter`` (when set) keeps only cases whose ``tier`` matches —
    used by the PR-triggered workflow to run the smoke set instead of the
    full suite. Cases without a ``tier`` field are treated as ``"full"``.
    """

    op: Literal["load_cases"]
    override_cases: list[dict[str, Any]] | None = None
    tier_filter: Literal["smoke", "full"] | None = None


class PassCriteria(BaseModel):
    """Per-case pass thresholds."""

    model_config = ConfigDict(extra="forbid", strict=True)

    min_task_count: int = Field(ge=1)
    max_task_count: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_duration_minutes: int = Field(ge=1)
    allow_rejections: bool = False


class CaseDef(BaseModel):
    """One case as it appears in cases.yaml."""

    model_config = ConfigDict(extra="forbid", strict=True)

    slug: str = Field(min_length=1, max_length=128)
    project_slug: str = Field(min_length=1, max_length=64)
    intent: str = Field(min_length=1, max_length=4096)
    tier: Literal["smoke", "full"] = "full"
    pass_criteria: PassCriteria


class EvaluateInput(BaseOp):
    """Compare an SDLC run's STATE against a case's pass criteria."""

    op: Literal["evaluate_result"]
    case: CaseDef
    run_id: str = Field(min_length=1, max_length=128)


class RecordInput(BaseOp):
    """Persist an evaluated result + emit per-case CloudWatch metrics."""

    op: Literal["record_result"]
    case_slug: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    passed: bool
    failures: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class AggregateInput(BaseOp):
    """Compute suite-wide pass rate from per-case results."""

    op: Literal["aggregate_results"]
    results: list[dict[str, Any]]


@cache
def s3() -> S3Client:
    """Process-cached S3 client."""
    return boto3.client("s3")


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def cw() -> CloudWatchClient:
    """Process-cached CloudWatch client."""
    return boto3.client("cloudwatch")


def artifacts_bucket() -> str:
    """Bucket holding ``evals/cases.yaml`` and ``evals/results/...``."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def runs_table() -> str:
    """Runs read-model table — the eval evaluator reads STATE rows from here."""
    return os.environ["AIDLC_RUNS_TABLE"]


def cases_key() -> str:
    """S3 key the case index is uploaded to."""
    return os.environ.get("AIDLC_EVAL_CASES_KEY", "evals/cases.yaml")


METRIC_NAMESPACE = "AIDLC/Evals"


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Lambda entrypoint dispatching on ``op``."""
    if not isinstance(event, dict):
        return error("invalid_event", "expected JSON object")
    op = event.get("op")
    if op == "load_cases":
        return load_cases(LoadCasesInput.model_validate(event))
    if op == "evaluate_result":
        return evaluate_result(EvaluateInput.model_validate(event))
    if op == "record_result":
        return record_result(RecordInput.model_validate(event))
    if op == "aggregate_results":
        return aggregate_results(AggregateInput.model_validate(event))
    return error(
        "unknown_op",
        f"op must be one of load_cases|evaluate_result|record_result|aggregate_results, got {op!r}",
    )


def load_cases(req: LoadCasesInput) -> dict[str, Any]:
    """Resolve the case list — either from the override or from S3, then filter."""
    if req.override_cases is not None:
        cases = [CaseDef.model_validate(c) for c in req.override_cases]
    else:
        body = s3().get_object(Bucket=artifacts_bucket(), Key=cases_key())["Body"].read()
        parsed = yaml.safe_load(body)
        raw = parsed.get("cases") or [] if isinstance(parsed, dict) else []
        cases = [CaseDef.model_validate(c) for c in raw]
    if req.tier_filter is not None:
        cases = [c for c in cases if c.tier == req.tier_filter]
    cases_dict = [c.model_dump() for c in cases]
    logger.info("cases loaded", extra={"count": len(cases_dict), "tier_filter": req.tier_filter})
    return {"ok": True, "cases": cases_dict}


def evaluate_result(req: EvaluateInput) -> dict[str, Any]:
    """Compare the run's STATE row against the case's pass criteria."""
    state = fetch_run_state(req.run_id)
    if state is None:
        return error("run_not_found", f"no STATE row for run_id={req.run_id}")
    metrics = state_to_metrics(state)
    failures = check_pass_criteria(metrics, req.case.pass_criteria)
    passed = not failures
    return {
        "ok": True,
        "case_slug": req.case.slug,
        "run_id": req.run_id,
        "passed": passed,
        "failures": failures,
        "metrics": metrics,
    }


def record_result(req: RecordInput) -> dict[str, Any]:
    """Persist the evaluated result to S3 and emit per-case CloudWatch metrics."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    key = f"evals/results/{date}/{req.case_slug}.json"
    record = {
        "schema_version": "1.0",
        "case_slug": req.case_slug,
        "run_id": req.run_id,
        "passed": req.passed,
        "failures": req.failures,
        "metrics": req.metrics,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    s3().put_object(
        Bucket=artifacts_bucket(),
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    cw().put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": "CaseResult",
                "Dimensions": [{"Name": "case_slug", "Value": req.case_slug}],
                "Value": 1.0 if req.passed else 0.0,
                "Unit": "Count",
            },
            {
                "MetricName": "CaseCost",
                "Dimensions": [{"Name": "case_slug", "Value": req.case_slug}],
                "Value": float(req.metrics.get("total_cost_usd", 0.0)),
                "Unit": "None",
            },
        ],
    )
    return {"ok": True, "case_slug": req.case_slug, "key": key}


def aggregate_results(req: AggregateInput) -> dict[str, Any]:
    """Compute suite-wide pass rate and emit the drift-alarm metric."""
    if not req.results:
        return {"ok": True, "pass_rate": 0.0, "passed": 0, "total": 0}
    passed = sum(1 for r in req.results if r.get("passed"))
    total = len(req.results)
    pass_rate = passed / total
    cw().put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {"MetricName": "PassRate", "Value": pass_rate, "Unit": "None"},
            {"MetricName": "PassCount", "Value": float(passed), "Unit": "Count"},
            {"MetricName": "TotalCount", "Value": float(total), "Unit": "Count"},
        ],
    )
    return {"ok": True, "pass_rate": pass_rate, "passed": passed, "total": total}


def fetch_run_state(run_id: str) -> dict[str, Any] | None:
    """Read the STATE row for ``run_id`` from the runs table."""
    resp = ddb().get_item(
        TableName=runs_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
    )
    return resp.get("Item")


def state_to_metrics(state: dict[str, Any]) -> dict[str, float]:
    """Extract the metrics we care about from a STATE row's DDB item."""
    return {
        "tasks_completed": float(_dn(state, "tasks_completed")),
        "task_count": float(_dn(state, "task_count") or _dn(state, "tasks_completed")),
        "total_cost_usd": float(_dn(state, "total_cost_usd")),
        "total_token_in": float(_dn(state, "total_token_in")),
        "total_token_out": float(_dn(state, "total_token_out")),
        "total_duration_ms": float(_dn(state, "total_duration_ms")),
        "total_rejections": float(_dn(state, "total_rejections")),
    }


def check_pass_criteria(metrics: dict[str, float], crit: PassCriteria) -> list[str]:
    """Return the list of failure reasons; empty list means the case passed."""
    failures: list[str] = []
    task_count = int(metrics.get("task_count") or metrics.get("tasks_completed") or 0)
    if task_count < crit.min_task_count:
        failures.append(f"task_count {task_count} < min {crit.min_task_count}")
    if task_count > crit.max_task_count:
        failures.append(f"task_count {task_count} > max {crit.max_task_count}")
    cost = metrics.get("total_cost_usd", 0.0)
    if cost > crit.max_cost_usd:
        failures.append(f"cost ${cost:.2f} > max ${crit.max_cost_usd:.2f}")
    duration_min = metrics.get("total_duration_ms", 0.0) / 60_000.0
    if duration_min > crit.max_duration_minutes:
        failures.append(
            f"duration {duration_min:.1f}m > max {crit.max_duration_minutes}m",
        )
    if not crit.allow_rejections and metrics.get("total_rejections", 0) > 0:
        failures.append(f"unexpected rejections: {int(metrics['total_rejections'])}")
    return failures


def error(kind: str, detail: object) -> dict[str, Any]:
    """Standard error envelope."""
    logger.warning("op rejected", extra={"kind": kind, "detail": detail})
    return {"ok": False, "error": {"kind": kind, "detail": detail}}


def _dn(state: dict[str, Any], attr: str) -> str:
    """Pull a Number-typed DDB attribute or default to ``"0"``."""
    return (state.get(attr) or {}).get("N", "0")
