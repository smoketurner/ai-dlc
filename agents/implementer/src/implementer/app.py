"""AgentCore Runtime entrypoint for the Implementer.

The state-router invokes this once per dispatch — first run
(``mode=implementation``) or revision (``mode=revision``). The
entrypoint:

  1. Validates the input as :class:`ImplementerInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the Claude Agent SDK session runs.
  3. Spawns a daemon thread that runs one Claude Agent SDK session
     and emits one of:

       * ``IMPL_PR.OPENED`` (implementation mode succeeded; PR open),
       * ``REVISION.READY`` (revision mode succeeded; impl branch
         pushed),
       * ``RUN.FAILED`` (uncaught exception or the agent produced no
         diff).

  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms so the state-router doesn't wait for the agent's full
     runtime and AgentCore's frontend never retries the dispatch.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, ImplPrOpened, RevisionReady, RunFailed
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import (
    ImplementerInput,
    ImplementerResult,
    ImplementerRevisionResult,
)
from implementer.client import execute_implementation, execute_revision

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = ImplementerInput.model_validate(event)
    logger.info(
        "implementer invoked",
        run_id=payload.run_id,
        mode=payload.mode,
        revision_number=payload.revision_number,
    )
    task_id = app.add_async_task(
        "implementer_run",
        {"run_id": payload.run_id, "mode": payload.mode},
    )
    threading.Thread(
        target=run_implementer,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {
        "status": "dispatched",
        "run_id": payload.run_id,
        "mode": payload.mode,
        "async_task_id": task_id,
    }


def run_implementer(payload: ImplementerInput, async_task_id: int) -> None:
    """Body of the implementer run — invokes Claude Agent SDK, emits event.

    Routes on ``payload.mode``: ``implementation`` emits IMPL_PR.OPENED;
    ``revision`` emits REVISION.READY. Uncaught exceptions surface as
    RUN.FAILED so the state machine never wedges.
    """
    try:
        if payload.mode == "revision":
            revision_result = asyncio.run(execute_revision(payload))
            emit_revision_ready(payload, revision_result)
        else:
            result = asyncio.run(execute_implementation(payload))
            emit_impl_pr_opened(payload, result)
    except Exception as exc:
        logger.exception(
            "implementer run failed",
            run_id=payload.run_id,
            mode=payload.mode,
        )
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(async_task_id)


def emit_revision_ready(
    payload: ImplementerInput,
    result: ImplementerRevisionResult,
) -> None:
    """Emit REVISION.READY so the projector advances ``revising → validation_running``."""
    envelope = EventEnvelope[RevisionReady](
        event_id=new_event_id(),
        type="REVISION.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=RevisionReady(
            project_slug=payload.project_slug,
            pr_url=result.pr_url,
            diff_summary=result.diff_summary,
            revision_number=result.revision_number,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def emit_impl_pr_opened(payload: ImplementerInput, result: ImplementerResult) -> None:
    """Emit IMPL_PR.OPENED so the projector advances ``implementer_running → impl_pr_open``."""
    envelope = EventEnvelope[ImplPrOpened](
        event_id=new_event_id(),
        type="IMPL_PR.OPENED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=ImplPrOpened(
            project_slug=payload.project_slug,
            pr_url=result.pr_url,
            diff_summary=result.diff_summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def publish_run_failed(payload: ImplementerInput, exc: BaseException) -> None:
    """Emit RUN.FAILED on uncaught exception in the agent body."""
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="implementer_running" if payload.mode == "implementation" else "revising",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
