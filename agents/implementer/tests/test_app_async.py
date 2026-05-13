"""The Implementer entrypoint dispatches work under a copied context.

Pins the AgentCore async-task contract: ``handler()`` returns
``{"status": "dispatched", ...}`` immediately and spawns the body
under a copied ``contextvars.Context`` so the runtime's
``WorkloadAccessToken`` reaches the daemon (the gateway path — both
the in-loop MCP server in ``options.py`` and the post-agent
``invoke_repo_helper`` calls — depends on it).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import ImplementerInput
from implementer import app


def make_input() -> ImplementerInput:
    return ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="implementation",
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
    assert out["async_task_id"] == 42
    assert captured["started"] is True
    assert captured["daemon"] is True
    # target is the bound `Context.run` method; args carry (run_implementer, payload, task_id).
    assert captured["target"].__name__ == "run"
    assert captured["args"][0] is app.run_implementer
    assert isinstance(captured["args"][1], ImplementerInput)
    assert captured["args"][2] == 42
