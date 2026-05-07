"""Strands tools the Proposer uses to read recent quality signals.

The Proposer reads from S3 (eval results, drift reports, rejection records
from the telemetry Lambda, and the few-shot example bank from the
few_shot_miner Lambda) and browses external best-practice docs via an
AgentCore browser session. It never reads or writes the SDLC pipeline
state directly.

S3 outputs are JSON-serialised summaries kept under ~16 KB so they fit
comfortably in the model's context. Browser results are also truncated.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from strands import tool

from common import agentcore_browser as browser
from common.errors import AgentCoreBrowserError
from common.memory_md import read_memory_md

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = structlog.get_logger()

BROWSER_GOTO_TIMEOUT_MS = 30_000
BROWSER_TEXT_LIMIT = 32_768

EVALS_RESULTS_PREFIX = "evals/results/"
EVALS_DRIFT_PREFIX = "evals/drift/"
EVALS_REJECTIONS_PREFIX = "evals/rejections/"
EVALS_FEW_SHOTS_PREFIX = "evals/few-shots/"
DEFAULT_LOOKBACK_DAYS = 30
SAMPLE_RUN_LIMIT = 5
FEW_SHOT_KEY_MIN_PARTS = 3


@cache
def s3_client() -> S3Client:
    """Process-cached S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding the eval substrate."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def list_recent(prefix: str, *, days: int) -> list[str]:
    """List object keys under ``prefix`` modified in the last ``days`` days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    bucket = artifacts_bucket()
    out: list[str] = []
    paginator = s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            last_modified = obj.get("LastModified")
            if last_modified is None or last_modified >= cutoff:
                out.append(obj["Key"])
    return out


def read_json(key: str) -> dict[str, Any] | None:
    """Read + parse a JSON object from the artifacts bucket; return None on failure."""
    try:
        body = s3_client().get_object(Bucket=artifacts_bucket(), Key=key)["Body"].read()
        return json.loads(body)
    except Exception:
        return None


def read_eval_aggregate(days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    """Aggregate per-case pass rates from the last ``days`` days of eval results."""
    keys = list_recent(EVALS_RESULTS_PREFIX, days=days)
    by_case: dict[str, dict[str, int]] = {}
    for key in keys:
        record = read_json(key)
        if not isinstance(record, dict):
            continue
        case_slug = record.get("case_slug")
        if not isinstance(case_slug, str):
            continue
        bucket = by_case.setdefault(case_slug, {"total": 0, "passed": 0})
        bucket["total"] += 1
        if record.get("passed"):
            bucket["passed"] += 1
    return {
        "lookback_days": days,
        "case_count": len(by_case),
        "per_case": [
            {
                "case_slug": case,
                "total": stats["total"],
                "passed": stats["passed"],
                "pass_rate": (stats["passed"] / stats["total"]) if stats["total"] else 0.0,
            }
            for case, stats in sorted(by_case.items())
        ],
    }


def read_drift_report(*, days: int = 7) -> dict[str, Any]:
    """Return the most recent drift report within ``days`` days, or an empty dict."""
    keys = sorted(list_recent(EVALS_DRIFT_PREFIX, days=days), reverse=True)
    for key in keys:
        record = read_json(key)
        if isinstance(record, dict):
            return record
    return {}


def read_rejection_summary(*, days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    """Aggregate rejection-category counts from the telemetry Lambda's S3 records."""
    keys = list_recent(EVALS_REJECTIONS_PREFIX, days=days)
    counter: Counter[str] = Counter()
    sample_runs: list[str] = []
    for key in keys:
        record = read_json(key)
        if not isinstance(record, dict):
            continue
        category = record.get("category")
        if isinstance(category, str):
            counter[category] += 1
        run_id = record.get("run_id")
        if isinstance(run_id, str) and len(sample_runs) < SAMPLE_RUN_LIMIT:
            sample_runs.append(run_id)
    return {
        "lookback_days": days,
        "total_rejections": sum(counter.values()),
        "by_category": [{"category": cat, "count": count} for cat, count in counter.most_common()],
        "sample_run_ids": sample_runs,
    }


def read_few_shot_summary(*, days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    """Count few-shot examples by kind (intent_to_spec / task_to_diff)."""
    keys = list_recent(EVALS_FEW_SHOTS_PREFIX, days=days)
    by_kind: Counter[str] = Counter()
    for key in keys:
        # Keys look like evals/few-shots/{kind}/{date}/{run_id}/{ix}.json
        parts = key.split("/")
        if len(parts) >= FEW_SHOT_KEY_MIN_PARTS:
            by_kind[parts[2]] += 1
    return {
        "lookback_days": days,
        "total_examples": sum(by_kind.values()),
        "by_kind": [{"kind": k, "count": c} for k, c in by_kind.most_common()],
    }


def aws_region() -> str:
    """AWS region the agent runtime is deployed in."""
    return os.environ["AWS_REGION"]


def browser_id() -> str | None:
    """AgentCore Browser resource id — None when unset."""
    return os.environ.get("AIDLC_BROWSER_ID") or None


def browse_url(url: str, extract_js: str | None = None) -> dict[str, Any]:
    """Fetch a page via an isolated AgentCore browser session.

    Use this to research external best-practices, framework docs, and
    library conventions while drafting a proposal. Avoid Google (cloud
    IPs hit CAPTCHAs) — prefer DuckDuckGo or Bing for general queries
    and fetch known doc domains directly (anthropic.com, OWASP,
    npmjs.com, pypi.org, GitHub READMEs).

    Args:
        url: Absolute URL to navigate to (must include scheme).
        extract_js: Optional JavaScript expression evaluated in the page
            after load. The return value MUST be JSON-serialisable.
            When omitted, the page's visible body text is returned.

    Returns:
        ``{"url": str, "title": str, "text": str}`` by default;
        ``{"url": str, "title": str, "extracted": Any}`` when
        ``extract_js`` is supplied. ``{"error": str}`` on failure.
    """
    bid = browser_id()
    if bid is None:
        return {"error": "AIDLC_BROWSER_ID is not set"}
    sdk_client = BrowserClient(region=aws_region())
    try:
        info = browser.start_session(sdk_client, browser_id=bid)
    except AgentCoreBrowserError as exc:
        return {"error": str(exc)}
    try:
        return navigate_and_extract(
            ws_url=info.ws_url,
            ws_headers=info.ws_headers,
            url=url,
            extract_js=extract_js,
        )
    finally:
        try:
            browser.stop_session(sdk_client)
        except AgentCoreBrowserError as exc:
            logger.warning("browser stop_session failed", err=str(exc))


def navigate_and_extract(
    *,
    ws_url: str,
    ws_headers: dict[str, str],
    url: str,
    extract_js: str | None,
) -> dict[str, Any]:
    """Connect Playwright to the running session and extract page content.

    Split out from :func:`browse_url` so tests can drive it without
    starting a real AgentCore session.
    """
    try:
        with sync_playwright() as runner:
            chromium = runner.chromium.connect_over_cdp(ws_url, headers=ws_headers)
            try:
                context = chromium.contexts[0] if chromium.contexts else chromium.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="load", timeout=BROWSER_GOTO_TIMEOUT_MS)
                title = page.title()
                if extract_js is not None:
                    extracted = page.evaluate(extract_js)
                    return {"url": url, "title": title, "extracted": extracted}
                text = page.evaluate("() => document.body.innerText")
                return {"url": url, "title": title, "text": str(text)[:BROWSER_TEXT_LIMIT]}
            finally:
                chromium.close()
    except PlaywrightError as exc:
        logger.warning("browser navigation failed", url=url, err=str(exc))
        return {"error": f"browse failed: {exc.__class__.__name__}: {exc}"}


# Strands wrappers — exposed to the agent.
read_eval_aggregate_tool = tool(read_eval_aggregate)
read_drift_report_tool = tool(read_drift_report)
read_rejection_summary_tool = tool(read_rejection_summary)
read_few_shot_summary_tool = tool(read_few_shot_summary)
read_memory_md_tool = tool(read_memory_md)
browse_url_tool = tool(browse_url)
