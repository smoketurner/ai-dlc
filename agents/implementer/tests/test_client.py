"""Tests for ``implementer.client.execute_implementation`` — single-PR flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import ImplementerInput
from implementer import client
from implementer.finish import FinishReport
from implementer.gates import CommandResult, GateResult
from implementer.repo_ops import RepoSession


@pytest.fixture
def payload() -> ImplementerInput:
    return ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="implementation",
        plan_s3_key="runs/01999999-9999-7999-9999-999999999999/plan.md",
        critique_s3_key="runs/01999999-9999-7999-9999-999999999999/critique.md",
        source_issue_url="https://github.com/owner/name/issues/42",
    )


@pytest.fixture
def fake_session() -> RepoSession:
    return RepoSession(
        target_repo="owner/name",
        access_token="ghs_test",  # noqa: S106 - fixture-only fake token
        author_login="ai-dlc[bot]",
        author_email="ai-dlc-bot@users.noreply.github.com",
        on_behalf_of_user=False,
    )


_PASSING_GATE = GateResult(passed=True, failures=[], all_results=[])
_FAILING_GATE = GateResult(
    passed=False,
    failures=[CommandResult(command="make lint", exit_code=1, stdout="", stderr="E501")],
    all_results=[CommandResult(command="make lint", exit_code=1, stdout="", stderr="E501")],
)


def _install_gate_mocks(
    monkeypatch: pytest.MonkeyPatch,
    calls: dict[str, list[Any]],
    *,
    drive_agent_report: FinishReport | None,
    gate_results: list[GateResult] | None,
    drive_agent_reports: list[FinishReport | None] | None,
) -> None:
    """Patch gate-related callables onto ``client``."""
    usage = {"token_in": 100, "token_out": 50, "cost_usd": 0.01, "duration_ms": 1234}
    _gate_seq = list(gate_results) if gate_results is not None else [_PASSING_GATE]
    _gate_iter = iter(_gate_seq)

    def fake_run_lint_gates(_cwd: Any) -> GateResult:
        try:
            return next(_gate_iter)
        except StopIteration:
            return _PASSING_GATE

    _remediation_reports = list(drive_agent_reports) if drive_agent_reports else []
    _remediation_iter = iter(_remediation_reports)
    _first_call = [True]

    async def fake_drive_agent(
        _prompt: str,
        *,
        run_id: str,
    ) -> tuple[FinishReport | None, dict[str, Any]]:
        del run_id
        if _first_call[0]:
            _first_call[0] = False
            return drive_agent_report, usage
        try:
            return next(_remediation_iter), usage
        except StopIteration:
            return drive_agent_report, usage

    def fake_write_blocked_md(gr: GateResult) -> None:
        calls["write_blocked_md"].append(gr)

    monkeypatch.setattr(client, "run_lint_gates", fake_run_lint_gates)
    monkeypatch.setattr(client, "drive_agent", fake_drive_agent)
    monkeypatch.setattr(client, "_write_blocked_md", fake_write_blocked_md)


def install_implementation_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_session: RepoSession,
    drive_agent_report: FinishReport | None,
    made_real_changes: bool,
    has_uncommitted_changes: bool,
    pr_url: str = "https://github.com/owner/name/pull/77",
    gate_results: list[GateResult] | None = None,
    drive_agent_reports: list[FinishReport | None] | None = None,
) -> dict[str, list[Any]]:
    """Wire the side-effecting helpers in ``execute_implementation`` to fakes.

    ``gate_results`` controls what ``run_lint_gates`` returns on successive calls
    (defaults to a single passing result).  ``drive_agent_reports`` controls what
    ``drive_agent`` returns on successive calls after the first (the first call
    always returns ``drive_agent_report``).
    """
    calls: dict[str, list[Any]] = {
        "clone_repo": [],
        "create_branch": [],
        "fetch_plan_and_critique": [],
        "commit_changes": [],
        "push_branch": [],
        "invoke_repo_helper": [],
        "write_blocked_md": [],
    }

    def fake_commit_changes(msg: str) -> str:
        calls["commit_changes"].append(msg)
        return "deadbeef"

    def fake_fetch(_mcp_client: Any, **kw: Any) -> None:
        calls["fetch_plan_and_critique"].append(kw)

    def fake_invoke_repo_helper(_mcp_client: Any, **kw: Any) -> dict[str, Any]:
        calls["invoke_repo_helper"].append(kw)
        if kw.get("op") == "open_pr":
            return {"pr_url": pr_url}
        return {}

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    monkeypatch.setattr(client, "gateway_mcp_client", lambda: fake_client)
    monkeypatch.setattr(client, "make_session", lambda **_: fake_session)
    monkeypatch.setattr(client, "clone_repo", calls["clone_repo"].append)
    monkeypatch.setattr(client, "create_branch", calls["create_branch"].append)
    monkeypatch.setattr(client, "fetch_plan_and_critique", fake_fetch)
    monkeypatch.setattr(client, "commit_changes", fake_commit_changes)
    monkeypatch.setattr(client, "push_branch", calls["push_branch"].append)
    monkeypatch.setattr(client, "invoke_repo_helper", fake_invoke_repo_helper)
    monkeypatch.setattr(client, "short_diff_summary", lambda: "diff stat")
    monkeypatch.setattr(client, "repo_made_real_changes", lambda: made_real_changes)
    monkeypatch.setattr(client, "has_uncommitted_changes", lambda: has_uncommitted_changes)
    monkeypatch.setattr(client, "repo_path", lambda: Path("/fake/repo"))
    _install_gate_mocks(
        monkeypatch,
        calls,
        drive_agent_report=drive_agent_report,
        gate_results=gate_results,
        drive_agent_reports=drive_agent_reports,
    )
    return calls


@pytest.mark.asyncio
async def test_execute_implementation_happy_path_opens_pr(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Successful run: commit, push, open_pr; result carries the PR URL."""
    report = FinishReport(summary="Added /healthz endpoint.", status="done")
    calls = install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=True,
    )

    result = await client.execute_implementation(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/77"
    assert calls["create_branch"] == ["aidlc/impl/01999999-9999-7999-9999-999999999999"]
    assert calls["fetch_plan_and_critique"][0]["plan_s3_key"] == payload.plan_s3_key
    assert calls["commit_changes"], "agent commit was skipped"
    assert calls["push_branch"] == ["aidlc/impl/01999999-9999-7999-9999-999999999999"]
    opens = [c for c in calls["invoke_repo_helper"] if c["op"] == "open_pr"]
    assert len(opens) == 1
    assert opens[0]["head"] == "aidlc/impl/01999999-9999-7999-9999-999999999999"
    assert opens[0]["base"] == "main"
    # PR body links the source issue so merging auto-closes it.
    assert "Closes https://github.com/owner/name/issues/42" in opens[0]["body"]


@pytest.mark.asyncio
async def test_execute_implementation_no_diff_raises(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Agent makes no real diff → RuntimeError; no PR opened."""
    install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=FinishReport(summary="Nothing to do.", status="done"),
        made_real_changes=False,
        has_uncommitted_changes=False,
    )

    with pytest.raises(RuntimeError, match="no diff"):
        await client.execute_implementation(payload)


@pytest.mark.asyncio
async def test_execute_implementation_blocked_finish_raises(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Agent calls finish with status='blocked' → RuntimeError surfacing the reason."""
    report = FinishReport(
        summary="Could not proceed.",
        status="blocked",
        blocked_reason="Plan was contradictory.",
    )
    install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=True,
    )

    with pytest.raises(RuntimeError, match="Plan was contradictory"):
        await client.execute_implementation(payload)


def test_render_pr_body_includes_summary_and_issue_link() -> None:
    """The PR body picks up the agent's summary and Closes <issue>."""
    report = FinishReport(
        summary="Added /healthz route + unit test.",
        files_changed=["app.py", "tests/test_health.py"],
        risks=["depends on FastAPI startup ordering"],
        status="done",
    )
    body = client.render_pr_body(
        report=report,
        run_id="r-1",
        source_issue_url="https://github.com/owner/name/issues/9",
        source_issue_title="Add /healthz route",
        intent="Add /healthz route",
    )
    assert "## Summary" in body
    assert "Added /healthz route" in body
    assert "Closes https://github.com/owner/name/issues/9" in body
    assert "- `app.py`" in body
    assert "## Residual risks" in body
    # Run ID hidden in HTML comment trailer (not visible to readers).
    assert "<!-- ai-dlc-run: r-1 -->" in body
    assert "Run: `r-1`" not in body


def test_render_pr_body_falls_back_to_issue_title_when_no_report() -> None:
    """No finish report → summary section uses the issue title as fallback."""
    body = client.render_pr_body(
        report=None,
        run_id="r-2",
        source_issue_url="https://github.com/owner/name/issues/9",
        source_issue_title="Add deterministic lint gates",
        intent="anything",
    )
    assert "## Summary" in body
    assert "Add deterministic lint gates" in body
    assert "Closes https://github.com/owner/name/issues/9" in body
    assert "<!-- ai-dlc-run: r-2 -->" in body


def test_render_pr_body_falls_back_to_intent_for_dashboard_runs() -> None:
    """No report, no issue title → use the dashboard intent as the summary."""
    body = client.render_pr_body(
        report=None,
        run_id="r-3",
        source_issue_url=None,
        source_issue_title=None,
        intent="Investigate slow queries",
    )
    assert "## Summary" in body
    assert "Investigate slow queries" in body
    assert "Closes" not in body
    assert "<!-- ai-dlc-run: r-3 -->" in body


def test_pr_title_prefers_issue_title() -> None:
    """Issue title wins over the agent's finish summary."""
    report = FinishReport(summary="Done.", files_changed=[], status="done")
    title = client.pr_title(
        report=report,
        source_issue_title="Add deterministic lint gates",
        intent="something else",
    )
    assert title == "Add deterministic lint gates"


def test_pr_title_uses_finish_summary_when_no_issue() -> None:
    """No issue → first line of the agent's finish summary."""
    report = FinishReport(
        summary="Added /healthz route.\nMore details.",
        files_changed=[],
        status="done",
    )
    title = client.pr_title(report=report, source_issue_title=None, intent=None)
    assert title == "Added /healthz route."


def test_pr_title_uses_intent_when_nothing_else() -> None:
    """Last-resort fallback is the original intent (no run UUID)."""
    title = client.pr_title(
        report=None,
        source_issue_title=None,
        intent="Investigate slow queries on /metrics",
    )
    assert title == "Investigate slow queries on /metrics"


def test_pr_title_static_fallback_when_no_context() -> None:
    """Nothing available → a clean static string, never the run UUID."""
    title = client.pr_title(report=None, source_issue_title=None, intent=None)
    assert title == "ai-dlc: automated changes"


def test_pr_title_truncates_long_strings() -> None:
    long_title = "x" * 500
    title = client.pr_title(report=None, source_issue_title=long_title, intent=None)
    assert len(title) == 200


def test_compose_implementation_prompt_mentions_plan_and_critique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt threads through plan_s3_key + critique_s3_key + issue URL."""
    monkeypatch.setattr("implementer.client.agent_memory_preamble", lambda **_: "<<MEMORY>>")
    payload = ImplementerInput(
        project_slug="demo",
        run_id="r-1",
        correlation_id="c-1",
        target_repo="owner/repo",
        mode="implementation",
        plan_s3_key="runs/r-1/plan.md",
        critique_s3_key="runs/r-1/critique.md",
        source_issue_url="https://github.com/owner/repo/issues/3",
    )
    prompt = client.compose_implementation_prompt(payload)
    assert "<<MEMORY>>" in prompt
    assert "runs/r-1/plan.md" in prompt
    assert "runs/r-1/critique.md" in prompt
    assert "https://github.com/owner/repo/issues/3" in prompt
    assert "/workspace/spec/plan.md" in prompt
    assert "high-severity finding" in prompt.lower()


def test_fetch_revision_inputs_pulls_via_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_revision_inputs uses gateway artifact_tool.get_artifact for each source."""
    fetched: list[dict[str, Any]] = []

    def fake_call(_client: Any, *, op: str, **fields: Any) -> dict[str, Any]:
        fetched.append({"op": op, **fields})
        # Simulate one of the keys missing — helper should swallow.
        if fields.get("key", "").endswith("mention.md"):
            msg = "not found"
            raise RuntimeError(msg)
        return {
            "ok": True,
            "op": op,
            "result": {"key": fields["key"], "content": f"<body for {fields['key']}>"},
        }

    monkeypatch.setattr(client, "call_artifact_tool", fake_call)

    inputs = client.fetch_revision_inputs(MagicMock(), run_id="r-1", revision_number=0)

    assert {f["op"] for f in fetched} == {"get_artifact"}
    keys = [f["key"] for f in fetched]
    assert "runs/r-1/validation/review-r0.md" in keys
    assert "runs/r-1/validation/test_report-r0.md" in keys
    assert "runs/r-1/validation/critique-r0.md" in keys
    assert inputs["review"].startswith("<body for ")
    assert inputs["mention"] == ""  # raised → swallowed → empty


# ---------------------------------------------------------------------------
# Gate loop integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_implementation_gate_passes_first_try(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Gate passes on first check → push proceeds normally."""
    report = FinishReport(summary="Added /healthz endpoint.", status="done")
    calls = install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=True,
        gate_results=[_PASSING_GATE],
    )

    result = await client.execute_implementation(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/77"
    assert calls["push_branch"] == ["aidlc/impl/01999999-9999-7999-9999-999999999999"]
    assert calls["write_blocked_md"] == []


@pytest.mark.asyncio
async def test_execute_implementation_gate_one_remediation_pass(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Gate fails first, passes second → one remediation, push proceeds."""
    report = FinishReport(summary="Fixed lint.", status="done")
    calls = install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=False,
        gate_results=[_FAILING_GATE, _PASSING_GATE],
        drive_agent_reports=[report],
    )

    result = await client.execute_implementation(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/77"
    assert calls["push_branch"] == ["aidlc/impl/01999999-9999-7999-9999-999999999999"]
    assert calls["write_blocked_md"] == []


@pytest.mark.asyncio
async def test_execute_implementation_gate_exhausted_writes_blocked_md(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Gate fails all 3 passes → BLOCKED.md written, RuntimeError raised, push not called."""
    report = FinishReport(summary="Tried.", status="done")
    calls = install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=False,
        # 4 gate checks: initial + 3 remediation passes, all failing
        gate_results=[_FAILING_GATE, _FAILING_GATE, _FAILING_GATE, _FAILING_GATE],
        drive_agent_reports=[report, report, report],
    )

    with pytest.raises(RuntimeError, match="gate exhausted"):
        await client.execute_implementation(payload)

    assert calls["push_branch"] == [], "push_branch must not be called when gate is exhausted"
    assert len(calls["write_blocked_md"]) == 1
    assert calls["write_blocked_md"][0].passed is False


@pytest.mark.asyncio
async def test_execute_implementation_gate_per_command_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Verify gate failures from each of the four commands are surfaced correctly."""
    for failing_command in ("make test", "make lint", "make type", "make format"):
        failing_gate = GateResult(
            passed=False,
            failures=[CommandResult(command=failing_command, exit_code=1, stdout="", stderr="err")],
            all_results=[
                CommandResult(command=failing_command, exit_code=1, stdout="", stderr="err")
            ],
        )
        report = FinishReport(summary="Done.", status="done")
        calls = install_implementation_mocks(
            monkeypatch,
            fake_session=fake_session,
            drive_agent_report=report,
            made_real_changes=True,
            has_uncommitted_changes=False,
            # Fail 4 times so gate exhausts
            gate_results=[failing_gate] * 4,
            drive_agent_reports=[report, report, report],
        )

        with pytest.raises(RuntimeError, match="gate exhausted"):
            await client.execute_implementation(payload)

        assert calls["push_branch"] == []
        assert len(calls["write_blocked_md"]) == 1
        blocked_gate: GateResult = calls["write_blocked_md"][0]
        assert blocked_gate.failures[0].command == failing_command
