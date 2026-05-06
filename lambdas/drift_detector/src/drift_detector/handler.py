"""Drift detector — compares trailing-week eval pass rate against baseline.

Triggered after each eval run completes (via EventBridge on the eval state
machine's `ExecutionSucceeded` event) and on a daily schedule. Reads the
most recent eval result records from S3 (``evals/results/{date}/``),
computes the trailing-7-day pass rate against the trailing-30-day baseline,
and:

  * Records a structured drift report to ``evals/drift/{ts}.json``.
  * Publishes a "RegressionDetected" CloudWatch metric (1.0 on regression,
    0.0 otherwise) — the alerts module's alarm watches this.
  * Sends a structured message to the alerts SNS topic when a regression
    fires, listing the cases that dropped and the week-over-week numbers.

PR commenting on the offending change is intentionally NOT implemented;
that needs a git_sha → PR resolver primitive on ``repo_helper`` plus
threading the commit SHA through eval results.

@TODO: add ``repo_helper.resolve_pr`` (wraps
``GET /repos/{owner}/{repo}/commits/{sha}/pulls``), thread commit SHA
into eval result records, then post a PR comment from this handler when
drift fires on a SHA we can resolve.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch.client import CloudWatchClient
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_sns.client import SNSClient

logger = Logger(service="drift_detector")

METRIC_NAMESPACE = "AIDLC/Evals"
RESULTS_PREFIX = "evals/results/"
DRIFT_PREFIX = "evals/drift/"
TRAILING_WINDOW_DAYS = 7
BASELINE_WINDOW_DAYS = 30
MIN_BASELINE_RUNS = 10  # avoid noisy comparisons against tiny baselines
DEFAULT_REGRESSION_THRESHOLD = 0.15  # 15-point pass-rate drop


class CaseResult(BaseModel):
    """One persisted eval result loaded from S3."""

    model_config = ConfigDict(extra="ignore", strict=True)

    case_slug: str
    run_id: str
    passed: bool
    evaluated_at: datetime


class WindowStats(BaseModel):
    """Aggregated stats for one comparison window."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    window_days: int
    total_runs: int
    passed_runs: int
    pass_rate: float


class CaseDelta(BaseModel):
    """Per-case pass-rate change between the two windows."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    case_slug: str
    trailing_pass_rate: float
    baseline_pass_rate: float
    delta: float


class DriftReport(BaseModel):
    """Structured output of one drift evaluation cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    evaluated_at: datetime
    threshold: float
    trailing: WindowStats
    baseline: WindowStats
    overall_delta: float
    regression: bool
    insufficient_data: bool
    case_deltas: list[CaseDelta] = Field(default_factory=list)
    regressing_cases: list[CaseDelta] = Field(default_factory=list)


@cache
def s3() -> S3Client:
    """Process-cached S3 client."""
    return boto3.client("s3")


@cache
def cw() -> CloudWatchClient:
    """Process-cached CloudWatch client."""
    return boto3.client("cloudwatch")


@cache
def sns() -> SNSClient:
    """Process-cached SNS client."""
    return boto3.client("sns")


def artifacts_bucket() -> str:
    """Bucket holding ``evals/results/...`` records."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def alerts_topic_arn() -> str:
    """SNS topic regression alerts publish to."""
    return os.environ["AIDLC_ALERTS_TOPIC_ARN"]


def regression_threshold() -> float:
    """Pass-rate-drop threshold above which a regression is declared."""
    return float(os.environ.get("AIDLC_REGRESSION_THRESHOLD", DEFAULT_REGRESSION_THRESHOLD))


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Lambda entrypoint. The event is opaque — the Lambda always re-reads S3."""
    del event
    now = datetime.now(UTC)
    results = load_recent_results(now=now, days=BASELINE_WINDOW_DAYS)
    report = compute_drift(results=results, now=now, threshold=regression_threshold())
    persist_report(report, now=now)
    emit_metric(report)
    if report.regression:
        publish_alert(report)
    return {"ok": True, "regression": report.regression, "report": report.model_dump(mode="json")}


def load_recent_results(*, now: datetime, days: int) -> list[CaseResult]:
    """List + read result objects from the last ``days`` days under ``RESULTS_PREFIX``."""
    cutoff = now - timedelta(days=days)
    bucket = artifacts_bucket()
    out: list[CaseResult] = []
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=RESULTS_PREFIX):
        for obj in page.get("Contents", []) or []:
            last_modified = obj.get("LastModified")
            if last_modified is not None and last_modified < cutoff:
                continue
            body = s3().get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            try:
                out.append(CaseResult.model_validate(json.loads(body)))
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "skipped malformed eval record",
                    extra={"key": obj["Key"], "err": str(exc)},
                )
    return out


def compute_drift(*, results: list[CaseResult], now: datetime, threshold: float) -> DriftReport:
    """Bucket results into trailing + baseline windows and compare pass rates."""
    trailing_cutoff = now - timedelta(days=TRAILING_WINDOW_DAYS)
    trailing = [r for r in results if r.evaluated_at >= trailing_cutoff]
    baseline = [r for r in results if r.evaluated_at < trailing_cutoff]
    trailing_stats = window_stats(trailing, TRAILING_WINDOW_DAYS)
    baseline_stats = window_stats(baseline, BASELINE_WINDOW_DAYS - TRAILING_WINDOW_DAYS)
    insufficient = baseline_stats.total_runs < MIN_BASELINE_RUNS
    case_deltas = compute_case_deltas(trailing=trailing, baseline=baseline)
    regressing = [d for d in case_deltas if (-d.delta) >= threshold]
    overall_delta = trailing_stats.pass_rate - baseline_stats.pass_rate
    enough_trailing = len(trailing) >= MIN_BASELINE_RUNS // 2
    regression = (not insufficient) and (-overall_delta) >= threshold and enough_trailing
    return DriftReport(
        evaluated_at=now,
        threshold=threshold,
        trailing=trailing_stats,
        baseline=baseline_stats,
        overall_delta=overall_delta,
        regression=regression,
        insufficient_data=insufficient,
        case_deltas=case_deltas,
        regressing_cases=regressing,
    )


def window_stats(results: list[CaseResult], window_days: int) -> WindowStats:
    """Aggregate one window's results."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    pass_rate = (passed / total) if total else 0.0
    return WindowStats(
        window_days=window_days, total_runs=total, passed_runs=passed, pass_rate=pass_rate
    )


def compute_case_deltas(
    *, trailing: list[CaseResult], baseline: list[CaseResult]
) -> list[CaseDelta]:
    """Per-case pass-rate delta between trailing and baseline windows."""
    trailing_by_case = group_by_case(trailing)
    baseline_by_case = group_by_case(baseline)
    cases = sorted(trailing_by_case.keys() | baseline_by_case.keys())
    deltas: list[CaseDelta] = []
    for case in cases:
        t_runs = trailing_by_case.get(case, [])
        b_runs = baseline_by_case.get(case, [])
        if not b_runs:  # no baseline; skip — can't be a regression by definition
            continue
        t_rate = pass_rate_of(t_runs)
        b_rate = pass_rate_of(b_runs)
        deltas.append(
            CaseDelta(
                case_slug=case,
                trailing_pass_rate=t_rate,
                baseline_pass_rate=b_rate,
                delta=t_rate - b_rate,
            ),
        )
    return deltas


def group_by_case(results: list[CaseResult]) -> dict[str, list[CaseResult]]:
    """Bucket results by ``case_slug``."""
    out: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        out[r.case_slug].append(r)
    return out


def pass_rate_of(results: list[CaseResult]) -> float:
    """Pass rate of a list, or 0.0 for an empty list."""
    if not results:
        return 0.0
    return sum(1 for r in results if r.passed) / len(results)


def persist_report(report: DriftReport, *, now: datetime) -> None:
    """Write the drift report to S3 for human inspection / future analysis."""
    key = f"{DRIFT_PREFIX}{now.strftime('%Y-%m-%dT%H-%M-%SZ')}.json"
    s3().put_object(
        Bucket=artifacts_bucket(),
        Key=key,
        Body=report.model_dump_json().encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    logger.info("drift report persisted", extra={"key": key, "regression": report.regression})


def emit_metric(report: DriftReport) -> None:
    """Emit ``RegressionDetected`` (0/1) so the alerts module's alarm can fire."""
    cw().put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": "RegressionDetected",
                "Value": 1.0 if report.regression else 0.0,
                "Unit": "None",
            },
        ],
    )


def publish_alert(report: DriftReport) -> None:
    """Send a structured regression alert to the alerts SNS topic."""
    lines = [
        "ai-dlc eval pass-rate regression detected",
        "",
        f"Trailing {report.trailing.window_days}d: "
        f"{report.trailing.passed_runs}/{report.trailing.total_runs} "
        f"({report.trailing.pass_rate:.1%})",
        f"Baseline ({report.baseline.window_days}d earlier): "
        f"{report.baseline.passed_runs}/{report.baseline.total_runs} "
        f"({report.baseline.pass_rate:.1%})",
        f"Delta: {report.overall_delta:+.1%} (threshold: -{report.threshold:.0%})",
    ]
    if report.regressing_cases:
        lines += ["", "Cases regressing the most:"]
        for d in sorted(report.regressing_cases, key=lambda c: c.delta)[:5]:
            lines.append(
                f"  - {d.case_slug}: {d.trailing_pass_rate:.0%} (was {d.baseline_pass_rate:.0%})"
            )
    sns().publish(
        TopicArn=alerts_topic_arn(),
        Subject="ai-dlc eval pass-rate regression",
        Message="\n".join(lines),
    )
    logger.info("regression alert published", extra={"delta": report.overall_delta})
