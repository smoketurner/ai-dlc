"""Read run artifacts (critique, spec docs) from the artifacts S3 bucket."""

from __future__ import annotations

import re

import structlog
from botocore.exceptions import ClientError
from markdown_it import MarkdownIt

from dashboard.deps import s3, settings
from dashboard.models import Critique

logger = structlog.get_logger()

CRITIQUE_KEY_TEMPLATE = "runs/{run_id}/critique.md"

SEVERITY_HEADER_RE = re.compile(
    r"^>\s*Issues:\s*\*\*(\d+)\*\*\s*high.*?\*\*(\d+)\*\*\s*medium.*?\*\*(\d+)\*\*\s*low",
    re.IGNORECASE | re.MULTILINE,
)


def read_critique(run_id: str) -> Critique | None:
    """Fetch and parse the critique markdown for a run.

    Args:
        run_id: The run identifier whose ``runs/<run_id>/critique.md`` to read.

    Returns:
        A populated :class:`Critique` when the object exists, ``None`` if the
        key isn't present yet (the critic hasn't run, or this run isn't gated
        by one).
    """
    cfg = settings()
    key = CRITIQUE_KEY_TEMPLATE.format(run_id=run_id)
    try:
        resp = s3().get_object(Bucket=cfg.artifacts_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        logger.warning("critique read failed", run_id=run_id, key=key, error=str(exc))
        raise
    body_md = resp["Body"].read().decode("utf-8")
    return parse_critique_md(body_md)


def parse_critique_md(body_md: str) -> Critique:
    """Render the critique markdown to HTML and extract severity counts."""
    high, medium, low = extract_severity_counts(body_md)
    summary = extract_summary(body_md)
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    body_html = md.render(body_md)
    return Critique(
        summary=summary,
        issue_count=high + medium + low,
        high_severity_count=high,
        medium_severity_count=medium,
        low_severity_count=low,
        body_html=body_html,
    )


def extract_severity_counts(body_md: str) -> tuple[int, int, int]:
    """Pull ``high·medium·low`` counts from the critique header line."""
    match = SEVERITY_HEADER_RE.search(body_md)
    if match is None:
        return 0, 0, 0
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def extract_summary(body_md: str) -> str:
    """First paragraph after the ``## Summary`` header, or empty string."""
    lines = body_md.splitlines()
    in_summary = False
    collected: list[str] = []
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            if line.strip():
                collected.append(line.strip())
            elif collected:
                break
    return " ".join(collected)
