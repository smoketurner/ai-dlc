"""Tests for ``agent_memory_preamble`` and ``render_memory_preamble``."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from common.agentcore_memory import MemoryRecord
from common.errors import AgentCoreMemoryError
from common.memory import (
    agent_memory_preamble,
    memory_client,
    render_memory_preamble,
)


@pytest.fixture(autouse=True)
def reset_client_cache() -> Iterator[None]:
    """The process-cached client must not leak between tests."""
    memory_client.cache_clear()
    yield
    memory_client.cache_clear()


def test_render_empty_records_returns_empty() -> None:
    assert render_memory_preamble([]) == ""


def test_render_records_emits_markdown_block() -> None:
    records = [
        MemoryRecord(
            record_id="r1",
            namespace="/projects/demo/facts",
            content="Use FastAPI + Jinja2 for the dashboard.",
            score=0.9,
        ),
        MemoryRecord(
            record_id="r2",
            namespace="/projects/demo/facts",
            content="ECS Fargate behind an ALB; not App Runner.",
            score=0.85,
        ),
    ]

    out = render_memory_preamble(records)

    assert out.startswith("## Recent project context")
    assert "FastAPI + Jinja2" in out
    assert "ECS Fargate" in out
    assert out.endswith("---\n")


def test_render_skips_blank_content() -> None:
    records = [
        MemoryRecord(record_id="r1", namespace="ns", content="real fact", score=0.9),
        MemoryRecord(record_id="r2", namespace="ns", content="   ", score=0.8),
    ]
    out = render_memory_preamble(records)
    assert "real fact" in out
    assert out.count("- ") == 1


def test_preamble_returns_empty_when_memory_id_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIDLC_MEMORY_ID", raising=False)

    out = agent_memory_preamble(project_slug="demo", query="anything")

    assert out == ""


def test_preamble_passes_namespace_and_query_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDLC_MEMORY_ID", "mem-1")
    fake_client = MagicMock()
    fake_client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {"memoryRecordId": "r1", "content": {"text": "fact one"}, "score": 0.9},
        ],
    }

    out = agent_memory_preamble(
        project_slug="demo",
        query="add /healthz endpoint",
        top_k=5,
        client=fake_client,
    )

    fake_client.retrieve_memory_records.assert_called_once_with(
        memoryId="mem-1",
        namespace="/projects/demo/facts",
        searchCriteria={"searchQuery": "add /healthz endpoint", "topK": 5},
    )
    assert "fact one" in out
    assert "## Recent project context" in out


def test_preamble_returns_empty_on_retrieval_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory-store outages must not gate the agent."""
    monkeypatch.setenv("AIDLC_MEMORY_ID", "mem-1")
    fake_client = MagicMock()
    fake_client.retrieve_memory_records.side_effect = (
        # The wrapper converts boto exceptions into AgentCoreMemoryError —
        # but here we simulate the wrapper-level failure directly to keep
        # the test focused on the catch-and-return-empty contract.
        _make_memory_error()
    )

    out = agent_memory_preamble(
        project_slug="demo",
        query="anything",
        client=fake_client,
    )

    assert out == ""


def _make_memory_error() -> AgentCoreMemoryError:
    return AgentCoreMemoryError("boom", memory_id="mem-1", namespace="/projects/demo/facts")


def test_preamble_returns_empty_when_no_records_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDLC_MEMORY_ID", "mem-1")
    fake_client = MagicMock()
    fake_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}

    out = agent_memory_preamble(
        project_slug="demo",
        query="anything",
        client=fake_client,
    )

    assert out == ""
