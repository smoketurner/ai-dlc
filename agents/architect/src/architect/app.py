"""AgentCore Runtime entrypoint for the Architect.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The state-router
Lambda invokes this runtime fire-and-forget when a run reaches
``architect_running`` / ``spec_pending``. The entrypoint:

  1. Validates the input as :class:`ArchitectInput`.
  2. Asks the Strands agent for a :class:`SpecBundle`.
  3. Renders the three Markdown docs and uploads each to S3 (via the
     agent's own ``write_spec_doc`` tool — but we also call the renderer
     directly here so the run is deterministic even if the model forgets
     to call the tool).
  4. Emits ``SPEC.READY`` so the projector advances the run, then
     returns the result body.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from architect.agent import build_agent, generate_spec, model_id
from architect.repo_grounding import clone_target_repo, sync_memory_md_from_clone
from architect.spec import SpecBundle, render_design, render_requirements, render_tasks
from architect.tools import write_spec_doc
from common.event_emit import publish
from common.events import EventEnvelope, SpecReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ArchitectInput, ArchitectResult, usage_from_strands

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Architect entrypoint. Returns a JSON-serialisable ArchitectResult."""
    payload = ArchitectInput.model_validate(event)
    logger.info(
        "architect invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        retry=payload.prior_feedback is not None,
    )

    clone_target_repo(payload.target_repo, requestor_sub=payload.requestor_sub)
    sync_memory_md_from_clone(
        project_slug=payload.project_slug,
        target_repo=payload.target_repo,
    )
    agent = build_agent(payload.run_id)
    spec = generate_spec(
        agent,
        payload.intent,
        project_slug=payload.project_slug,
        prior_feedback=payload.prior_feedback,
    )
    upload_spec(spec)

    result = ArchitectResult(
        spec_slug=spec.spec_slug,
        spec_s3_prefix=f"specs/{spec.spec_slug}/",
        requirements_summary=spec.requirements.summary[:1024],
        design_summary=spec.design.approach[:1024],
        task_count=len(spec.tasks),
        task_ids=[t.id for t in spec.tasks],
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
    return result.model_dump()


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
            proposed_adrs=list(result.proposed_adrs),
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
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
