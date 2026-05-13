"""AgentCore Runtime entrypoint for the Architect.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The state-router
Lambda invokes this runtime when a run reaches ``architect_running``. The
entrypoint:

  1. Validates the input as :class:`ArchitectInput`.
  2. Registers an async task with the AgentCore SDK so the runtime's
     ``/ping`` reports ``HealthyBusy`` until the work finishes.
  3. Spawns a daemon thread that does the actual work — clone the
     target repo, generate the plan, upload it to S3, emit
     ``DESIGN.READY`` — then acknowledges the async task so the
     microVM goes idle.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms so the state-router doesn't hold a Lambda for the agent's
     full runtime and AgentCore's frontend never sees a dropped
     connection (which it would otherwise retry as a duplicate
     invoke).
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from architect.agent import build_agent, generate_plan, model_id
from architect.plan import extract_proposed_adrs, extract_summary
from architect.repo_grounding import (
    clone_target_repo,
    sync_memory_md_from_clone,
    sync_stack_profile_from_clone,
)
from architect.tools import plan_s3_key, read_plan_doc
from common.event_emit import publish
from common.events import DesignReady, EventEnvelope, RunFailed
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ArchitectInput, ArchitectResult, usage_from_strands

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately.

    The agent body runs on a daemon thread; AgentCore keeps the
    microVM alive while ``add_async_task`` is open and tears it down
    after ``complete_async_task`` has been called and the SDK's idle
    timeout elapses.
    """
    payload = ArchitectInput.model_validate(event)
    logger.info(
        "architect invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        source_issue_url=payload.source_issue_url,
    )
    task_id = app.add_async_task("architect_run", {"run_id": payload.run_id})
    threading.Thread(
        target=run_architect,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_architect(payload: ArchitectInput, task_id: int) -> None:
    """Body of the architect run — clones repo, generates plan, emits event.

    Runs in a daemon thread spawned from :func:`handler`. Always emits
    a terminal event (``DESIGN.READY`` on success, ``RUN.FAILED`` on
    exception) so the state machine advances rather than wedging.
    """
    try:
        clone_target_repo(payload.target_repo, requestor_sub=payload.requestor_sub)
        sync_memory_md_from_clone(
            project_slug=payload.project_slug,
            target_repo=payload.target_repo,
        )
        sync_stack_profile_from_clone(project_slug=payload.project_slug)
        agent = build_agent(payload.run_id)
        generate_plan(
            agent,
            project_slug=payload.project_slug,
            run_id=payload.run_id,
            intent=payload.intent,
            triggering_comment_body=payload.triggering_comment_body,
            source_issue_url=payload.source_issue_url,
            source_issue_title=payload.source_issue_title,
            source_issue_body=payload.source_issue_body,
        )
        plan_body = read_plan_doc(payload.run_id)
        result = ArchitectResult(
            plan_s3_key=plan_s3_key(payload.run_id),
            summary=extract_summary(plan_body),
            proposed_adrs=extract_proposed_adrs(plan_body),
            session_id=payload.run_id,
            **usage_from_strands(agent, model_id=model_id()),
        )
        logger.info(
            "plan ready",
            run_id=payload.run_id,
            plan_s3_key=result.plan_s3_key,
            proposed_adr_count=len(result.proposed_adrs),
        )
        publish_design_ready(payload, result)
    except Exception as exc:
        logger.exception("architect run failed", run_id=payload.run_id)
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(task_id)


def publish_design_ready(payload: ArchitectInput, result: ArchitectResult) -> None:
    """Emit DESIGN.READY so the projector advances the run to ``designed``."""
    envelope = EventEnvelope[DesignReady](
        event_id=new_event_id(),
        type="DESIGN.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="architect",
        payload=DesignReady(
            project_slug=payload.project_slug,
            plan_s3_key=result.plan_s3_key,
            summary=result.summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def publish_run_failed(payload: ArchitectInput, exc: BaseException) -> None:
    """Emit RUN.FAILED so the projector terminates the run on agent crash.

    Without this, a thrown exception inside the background thread would
    leave the run wedged in ``architect_running`` with nothing else to
    advance it — the state-router has already reported a successful
    dispatch.
    """
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="architect",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="architect_running",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
