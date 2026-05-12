"""AgentCore Runtime entrypoint for the Architect.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The state-router
Lambda invokes this runtime when a run reaches ``architect_running`` /
``spec_pending``. The entrypoint:

  1. Validates the input as :class:`ArchitectInput`.
  2. Registers an async task with the AgentCore SDK so the runtime's
     ``/ping`` reports ``HealthyBusy`` until the work finishes.
  3. Spawns a daemon thread that does the actual work — clone the
     target repo, generate the spec, upload it to S3, emit
     ``SPEC.READY`` — then acknowledges the async task so the
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

from architect.agent import build_agent, generate_spec, model_id
from architect.repo_grounding import (
    clone_target_repo,
    sync_memory_md_from_clone,
    sync_stack_profile_from_clone,
)
from architect.spec import SpecBundle, render_design, render_requirements, render_tasks
from architect.tools import write_spec_doc
from common.event_emit import publish
from common.events import EventEnvelope, RunFailed, SpecReady
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
        retry=payload.prior_feedback is not None,
    )
    task_id = app.add_async_task("architect_run", {"run_id": payload.run_id})
    threading.Thread(
        target=run_architect,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_architect(payload: ArchitectInput, task_id: int) -> None:
    """Body of the architect run — clones repo, generates spec, emits event.

    Runs in a daemon thread spawned from :func:`handler`. Always emits
    a terminal event (``SPEC.READY`` on success, ``RUN.FAILED`` on
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
        spec = generate_spec(
            agent,
            payload.intent,
            project_slug=payload.project_slug,
            prior_feedback=payload.prior_feedback,
            triggering_comment_body=payload.triggering_comment_body,
        )
        upload_spec(spec)

        result = ArchitectResult(
            spec_slug=spec.spec_slug,
            spec_s3_prefix=f"specs/{spec.spec_slug}/",
            requirements_summary=spec.requirements.summary[:1024],
            design_summary=spec.design.approach[:1024],
            task_count=len(spec.tasks),
            task_ids=[t.id for t in spec.tasks],
            task_depends_on={
                t.id: list(t.depends_on) for t in spec.tasks if t.depends_on
            },
            one_way_task_count=count_one_way_tasks(spec),
            proposed_adrs=spec.design.proposed_adrs,
            session_id=payload.run_id,
            **usage_from_strands(agent, model_id=model_id()),
        )
        logger.info(
            "spec ready",
            run_id=payload.run_id,
            spec_slug=spec.spec_slug,
            task_count=result.task_count,
        )
        publish_spec_ready(payload, result)
    except Exception as exc:
        logger.exception("architect run failed", run_id=payload.run_id)
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(task_id)


def publish_spec_ready(payload: ArchitectInput, result: ArchitectResult) -> None:
    """Emit SPEC.READY so the projector advances the run to ``spec_drafted``."""
    envelope = EventEnvelope[SpecReady](
        event_id=new_event_id(),
        type="SPEC.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="architect",
        payload=SpecReady(
            project_slug=payload.project_slug,
            spec_slug=result.spec_slug,
            spec_s3_prefix=result.spec_s3_prefix,
            requirements_summary=result.requirements_summary,
            design_summary=result.design_summary,
            task_count=result.task_count,
            task_ids=list(result.task_ids),
            task_depends_on=dict(result.task_depends_on),
            proposed_adrs=list(result.proposed_adrs),
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


def upload_spec(spec: SpecBundle) -> None:
    """Render and upload the three spec docs to S3."""
    write_spec_doc(spec.spec_slug, "requirements", render_requirements(spec))
    write_spec_doc(spec.spec_slug, "design", render_design(spec))
    write_spec_doc(spec.spec_slug, "tasks", render_tasks(spec))


def count_one_way_tasks(spec: SpecBundle) -> int:
    """Number of tasks classified as one-way doors by the Architect."""
    return sum(1 for t in spec.tasks if t.door.door_class == "one_way")


if __name__ == "__main__":
    app.run()
