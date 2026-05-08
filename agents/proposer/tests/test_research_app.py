"""Tests for the Proposer's research-trigger path in ``app.py``.

The agent itself is mocked — we focus on the orchestration: that
``run_research`` posts a comment via ``repo_helper``, opens a PR only when
the proposal has edits, and emits ``RUN.COMPLETED`` so the projector can
advance the run state.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from common.runtime import ProposerInput
from proposer import app
from proposer.proposal import FileEdit, Proposal, ProposedIssue


def make_input(**overrides: Any) -> ProposerInput:
    base: dict[str, Any] = {
        "project_slug": "ai-dlc",
        "target_repo": "smoketurner/ai-dlc",
        "trigger_reason": "research",
        "intent": "what can we learn from https://example.com/post",
        "issue_number": 34,
        "run_id": "019e08a2-aaeb-75c1-b03e-a59ef84f1a1c",
        "correlation_id": "019e08a2-aaeb-75c1-b03e-a59ef84f1a20",
        "actor_id": "system",
    }
    base.update(overrides)
    return ProposerInput.model_validate(base)


def make_proposal(*, edits: list[FileEdit] | None = None, comment: str = "ok") -> Proposal:
    return Proposal(
        rationale="research synthesis",
        edits=edits or [],
        pr_title="proposer: research findings",
        pr_body="research findings synthesized from referenced URLs",
        summary_comment=comment,
    )


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", "ai-dlc-repo-helper")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_BROWSER_ID", "b-1")


def test_research_posts_comment_and_emits_run_completed() -> None:
    payload = make_input()
    proposal = make_proposal(comment="- adopt minion-style one-shot agents")
    with (
        patch("proposer.app.propose_research", return_value=proposal) as p_research,
        patch("proposer.app.invoke_repo_helper", return_value={"ok": True, "result": {}}) as p_repo,
        patch("proposer.app.publish") as p_publish,
    ):
        app.run_research(payload)

    p_research.assert_called_once()
    comment_call = p_repo.call_args
    assert comment_call.kwargs["op"] == "comment_issue"
    assert comment_call.kwargs["issue_number"] == 34
    assert "minion-style" in comment_call.kwargs["body"]
    p_publish.assert_called_once()
    envelope = p_publish.call_args.args[0]
    assert envelope.type == "RUN.COMPLETED"
    assert envelope.actor_id == "proposer"
    assert envelope.payload.tasks_completed == 0


def test_research_opens_pr_when_proposal_has_edits() -> None:
    payload = make_input()
    proposal = make_proposal(
        comment="findings",
        edits=[
            FileEdit(
                target_file="docs/MEMORY.md",
                proposed_content="# Conventions\n- new rule\n",
            )
        ],
    )

    open_pr_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
        open_pr_calls.append((op, fields))
        if op == "open_pr":
            return {"ok": True, "result": {"pr_url": "https://github.com/x/y/pull/1"}}
        return {"ok": True, "result": {}}

    with (
        patch("proposer.app.propose_research", return_value=proposal),
        patch("proposer.app.invoke_repo_helper", side_effect=fake_repo_helper),
        patch("proposer.app.publish") as p_publish,
    ):
        app.run_research(payload)

    ops = [op for op, _ in open_pr_calls]
    assert "comment_issue" in ops
    assert "create_branch" in ops
    assert "commit_files" in ops
    assert "open_pr" in ops
    p_publish.assert_called_once()
    assert p_publish.call_args.args[0].payload.tasks_completed == 1


def test_research_skips_comment_when_summary_empty() -> None:
    payload = make_input()
    proposal = make_proposal(comment="")
    with (
        patch("proposer.app.propose_research", return_value=proposal),
        patch("proposer.app.invoke_repo_helper") as p_repo,
        patch("proposer.app.publish") as p_publish,
    ):
        app.run_research(payload)

    p_repo.assert_not_called()
    p_publish.assert_called_once()


def test_research_requires_intent_and_issue_number() -> None:
    payload = make_input(intent=None)
    with (
        patch("proposer.app.propose_research") as p_research,
        patch("proposer.app.invoke_repo_helper"),
        patch("proposer.app.publish"),
        pytest.raises(ValueError, match="intent"),
    ):
        app.run_research(payload)
    p_research.assert_not_called()


def test_run_proposer_routes_research_path() -> None:
    payload = make_input()
    with (
        patch("proposer.app.run_research") as p_research,
        patch("proposer.app.run_scheduled") as p_sched,
        patch.object(app.app, "complete_async_task") as p_done,
    ):
        app.run_proposer(payload, async_task_id=42)

    p_research.assert_called_once_with(payload)
    p_sched.assert_not_called()
    p_done.assert_called_once_with(42)


def test_run_proposer_routes_scheduled_path() -> None:
    payload = make_input(trigger_reason="scheduled", intent=None, issue_number=None)
    with (
        patch("proposer.app.run_research") as p_research,
        patch("proposer.app.run_scheduled") as p_sched,
        patch.object(app.app, "complete_async_task") as p_done,
    ):
        app.run_proposer(payload, async_task_id=7)

    p_research.assert_not_called()
    p_sched.assert_called_once_with(payload)
    p_done.assert_called_once_with(7)


def test_research_forwards_triggering_comment_to_agent() -> None:
    payload = make_input(
        triggering_comment_body="@aidlc-bot create issues for the top 2 adopt items",
        triggering_commenter="jplock",
    )
    proposal = make_proposal(comment="ok")
    with (
        patch("proposer.app.propose_research", return_value=proposal) as p_research,
        patch("proposer.app.invoke_repo_helper", return_value={"ok": True, "result": {}}),
        patch("proposer.app.publish"),
    ):
        app.run_research(payload)

    kwargs = p_research.call_args.kwargs
    assert kwargs["triggering_comment_body"] == (
        "@aidlc-bot create issues for the top 2 adopt items"
    )
    assert kwargs["triggering_commenter"] == "jplock"
    assert kwargs["target_repo"] == "smoketurner/ai-dlc"


def test_research_spawns_issues_from_proposed_issues() -> None:
    payload = make_input(
        triggering_comment_body="@aidlc-bot create issues for the highest-impact items",
        triggering_commenter="jplock",
    )
    proposal = Proposal(
        rationale="user asked for issue creation",
        summary_comment="Spawned 2 issues for the top adopt items.",
        proposed_issues=[
            ProposedIssue(
                title="Adopt scoped rule files split by directory",
                body="## Scope\nSplit MEMORY.md.",
                labels=["aidlc-spawned", "adopt"],
            ),
            ProposedIssue(
                title="Pre-warm sandbox snapshots",
                body="## Scope\nModal-style snapshots.",
                labels=["aidlc-spawned", "adopt"],
            ),
        ],
    )

    repo_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
        repo_calls.append((op, fields))
        if op == "create_issue":
            return {
                "ok": True,
                "result": {
                    "issue_number": 99,
                    "issue_url": f"https://github.com/x/y/issues/{len(repo_calls)}",
                    "state": "open",
                    "labels": list(fields.get("labels", [])),
                },
            }
        return {"ok": True, "result": {}}

    with (
        patch("proposer.app.propose_research", return_value=proposal),
        patch("proposer.app.invoke_repo_helper", side_effect=fake_repo_helper),
        patch("proposer.app.publish"),
    ):
        app.run_research(payload)

    create_calls = [fields for op, fields in repo_calls if op == "create_issue"]
    assert len(create_calls) == 2
    first = create_calls[0]
    assert first["repo"] == "smoketurner/ai-dlc"
    assert first["title"] == "Adopt scoped rule files split by directory"
    assert first["labels"] == ["aidlc-spawned", "adopt"]
    assert first["parent_issue_url"] == "https://github.com/smoketurner/ai-dlc/issues/34"
    assert first["requestor"] == "jplock"


def test_research_default_label_when_proposed_issue_omits_labels() -> None:
    payload = make_input(
        triggering_comment_body="@aidlc-bot please spawn one issue",
        triggering_commenter="jplock",
    )
    proposal = Proposal(
        rationale="single issue requested",
        summary_comment="ok",
        proposed_issues=[ProposedIssue(title="Adopt X", body="Body.")],
    )
    repo_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
        repo_calls.append((op, fields))
        if op == "create_issue":
            return {
                "ok": True,
                "result": {
                    "issue_number": 1,
                    "issue_url": "https://github.com/x/y/issues/1",
                    "state": "open",
                    "labels": fields.get("labels", []),
                },
            }
        return {"ok": True, "result": {}}

    with (
        patch("proposer.app.propose_research", return_value=proposal),
        patch("proposer.app.invoke_repo_helper", side_effect=fake_repo_helper),
        patch("proposer.app.publish"),
    ):
        app.run_research(payload)

    create_calls = [fields for op, fields in repo_calls if op == "create_issue"]
    assert create_calls[0]["labels"] == ["aidlc-spawned"]


def test_research_skips_create_issue_when_proposed_issues_empty() -> None:
    payload = make_input()
    proposal = make_proposal(comment="findings")  # no proposed_issues
    repo_calls: list[str] = []

    def fake_repo_helper(*, op: str, **_fields: Any) -> dict[str, Any]:
        repo_calls.append(op)
        return {"ok": True, "result": {}}

    with (
        patch("proposer.app.propose_research", return_value=proposal),
        patch("proposer.app.invoke_repo_helper", side_effect=fake_repo_helper),
        patch("proposer.app.publish"),
    ):
        app.run_research(payload)

    assert "create_issue" not in repo_calls


def test_parent_issue_url_builds_from_payload() -> None:
    payload = make_input()
    assert app.parent_issue_url(payload) == "https://github.com/smoketurner/ai-dlc/issues/34"
