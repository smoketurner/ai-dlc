"""The Proposer entrypoint is non-blocking — work runs on a daemon thread.

Pins the AgentCore async-task contract: ``handler()`` returns
``{"status": "dispatched", ...}`` immediately and spawns the body
under a copied ``contextvars.Context`` so the runtime's
``WorkloadAccessToken`` reaches the daemon (the gateway tool path
depends on it).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import ProposerInput
from proposer import app


def make_input() -> ProposerInput:
    return ProposerInput(
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        trigger_reason="research",
        intent="https://example.com/post",
        issue_number=34,
        run_id="019e08a2-aaeb-75c1-b03e-a59ef84f1a1c",
        correlation_id="019e08a2-aaeb-75c1-b03e-a59ef84f1a20",
    )


def test_handler_dispatches_via_copy_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler spawns a daemon thread whose target runs under a copied context."""
    monkeypatch.setattr(app.app, "add_async_task", MagicMock(return_value=42))

    captured: dict[str, Any] = {}

    class FakeThread:
        def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(app.threading, "Thread", FakeThread)

    out = app.handler(make_input().model_dump())

    assert out["status"] == "dispatched"
    assert out["task_id"] == 42
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["target"].__name__ == "run"
    assert captured["args"][0] is app.run_proposer
    assert isinstance(captured["args"][1], ProposerInput)
    assert captured["args"][2] == 42
