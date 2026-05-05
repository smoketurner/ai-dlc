"""Tests for ``implementer.finish``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from implementer.finish import (
    FINISH_TOOL_NAME,
    FinishReport,
    FinishSink,
    TestResult,
    build_finish_server,
    build_finish_tool,
)


def test_finish_report_minimal_done() -> None:
    report = FinishReport(summary="Added /healthz endpoint.", status="done")
    assert report.status == "done"
    assert report.blocked_reason is None
    assert report.files_changed == []
    assert report.tests_run == []
    assert report.risks == []


def test_finish_report_full_done() -> None:
    report = FinishReport(
        summary="Added /healthz endpoint and unit tests.",
        files_changed=["app/main.py", "tests/test_health.py"],
        tests_run=[TestResult(name="test_health_returns_200", status="pass")],
        risks=["depends on FastAPI startup ordering"],
        status="done",
    )
    assert report.tests_run[0].status == "pass"


def test_finish_report_blocked_requires_reason() -> None:
    with pytest.raises(ValidationError):
        FinishReport(summary="Could not proceed.", status="blocked")


def test_finish_report_blocked_with_reason() -> None:
    report = FinishReport(
        summary="Could not proceed.",
        status="blocked",
        blocked_reason="Requirements doc references endpoint not in design.",
    )
    assert report.blocked_reason is not None


def test_finish_report_done_must_not_include_reason() -> None:
    with pytest.raises(ValidationError):
        FinishReport(summary="Done", status="done", blocked_reason="should not be here")


def test_finish_report_summary_max_500_chars() -> None:
    with pytest.raises(ValidationError):
        FinishReport(summary="x" * 501, status="done")


def test_finish_report_summary_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        FinishReport(summary="", status="done")


def test_finish_report_unknown_status_rejected() -> None:
    with pytest.raises(ValidationError):
        FinishReport.model_validate({"summary": "x", "status": "in-progress"})


def test_finish_report_files_changed_capped_at_64() -> None:
    with pytest.raises(ValidationError):
        FinishReport(
            summary="too many files",
            files_changed=[f"f{i}.py" for i in range(65)],
            status="done",
        )


def test_finish_report_risks_capped_at_8_and_each_256() -> None:
    with pytest.raises(ValidationError):
        FinishReport(summary="x", risks=[f"r{i}" for i in range(9)], status="done")
    with pytest.raises(ValidationError):
        FinishReport(summary="x", risks=["x" * 257], status="done")


def test_finish_report_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        FinishReport.model_validate({"summary": "x", "status": "done", "extra": 1})


def test_test_result_status_constrained() -> None:
    with pytest.raises(ValidationError):
        TestResult(name="t", status="error")  # ty: ignore[invalid-argument-type]


def test_finish_sink_stores_last_report() -> None:
    sink = FinishSink()
    assert sink.report is None
    first = FinishReport(summary="first", status="done")
    sink.set(first)
    assert sink.report is first
    second = FinishReport(summary="second", status="done")
    sink.set(second)
    assert sink.report is second


def test_finish_tool_name_is_canonical_mcp_form() -> None:
    assert FINISH_TOOL_NAME == "mcp__finish_server__finish"


def test_build_finish_tool_returns_sdk_tool() -> None:
    sink = FinishSink()
    sdk_tool = build_finish_tool(sink)
    assert sdk_tool.name == "finish"


def test_build_finish_server_returns_config() -> None:
    sink = FinishSink()
    config = build_finish_server(sink)
    assert config["type"] == "sdk"


@pytest.mark.asyncio
async def test_finish_tool_handler_writes_to_sink() -> None:
    sink = FinishSink()
    sdk_tool = build_finish_tool(sink)
    args: dict[str, object] = {
        "summary": "Done with /healthz",
        "files_changed": ["app/main.py"],
        "tests_run": [{"name": "test_health", "status": "pass"}],
        "risks": [],
        "status": "done",
    }
    result = await sdk_tool.handler(args)
    assert result.get("is_error") is not True
    assert sink.report is not None
    assert sink.report.summary == "Done with /healthz"


@pytest.mark.asyncio
async def test_finish_tool_handler_returns_error_on_invalid_args() -> None:
    sink = FinishSink()
    sdk_tool = build_finish_tool(sink)
    bad: dict[str, object] = {"summary": "x", "status": "blocked"}  # missing reason
    result = await sdk_tool.handler(bad)
    assert result.get("is_error") is True
    assert sink.report is None
