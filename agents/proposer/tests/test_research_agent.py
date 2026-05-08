"""Tests for the proposer's research-prompt composition.

The Strands agent itself is not exercised — these tests cover only the
deterministic prompt assembly so we can verify the follow-up-comment
section appears when (and only when) a triggering comment is supplied.
"""

from __future__ import annotations

from unittest.mock import patch

from proposer.agent import compose_research_message


@patch("proposer.agent.agent_memory_preamble", return_value="(memory preamble)")
def test_compose_research_message_omits_follow_up_when_no_comment(
    _mock_preamble: object,
) -> None:
    msg = compose_research_message(
        project_slug="ai-dlc",
        intent="https://example.com/post",
        issue_number=34,
        target_repo="smoketurner/ai-dlc",
    )
    assert "Follow-up comment" not in msg
    assert "This run was triggered by the follow-up comment" not in msg
    assert "proposed_issues" in msg  # default-empty guidance always present
    assert "Target repo: smoketurner/ai-dlc" in msg


@patch("proposer.agent.agent_memory_preamble", return_value="(memory preamble)")
def test_compose_research_message_includes_follow_up_block(
    _mock_preamble: object,
) -> None:
    msg = compose_research_message(
        project_slug="ai-dlc",
        intent="https://example.com/post",
        issue_number=34,
        target_repo="smoketurner/ai-dlc",
        triggering_comment_body="@aidlc-bot please create issues for the top items",
        triggering_commenter="jplock",
    )
    assert "Follow-up comment by @jplock" in msg
    assert "@aidlc-bot please create issues" in msg
    assert "list_issue_comments" in msg
    assert "proposed_issues" in msg


@patch("proposer.agent.agent_memory_preamble", return_value="(memory preamble)")
def test_compose_research_message_omits_attribution_without_commenter(
    _mock_preamble: object,
) -> None:
    msg = compose_research_message(
        project_slug="ai-dlc",
        intent="https://example.com/post",
        issue_number=34,
        triggering_comment_body="@aidlc-bot create issues",
    )
    assert "Follow-up comment:" in msg
    assert "Follow-up comment by @" not in msg
