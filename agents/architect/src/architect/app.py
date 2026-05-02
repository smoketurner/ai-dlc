"""AgentCore Runtime entrypoint for the Architect.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. Step Functions
calls the runtime, which invokes the entrypoint defined here. The
entrypoint:

  1. Validates the input as :class:`ArchitectInput`.
  2. Asks the Strands agent for a :class:`SpecBundle`.
  3. Renders the three Markdown docs and uploads each to S3 (via the
     agent's own ``write_spec_doc`` tool — but we also call the renderer
     directly here so the run is deterministic even if the model forgets
     to call the tool).
  4. Returns an :class:`ArchitectResult` for the SPEC.READY event payload.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from architect.agent import generate_spec
from architect.spec import SpecBundle, render_design, render_requirements, render_tasks
from architect.tools import write_spec_doc
from common.runtime import ArchitectInput, ArchitectResult

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

    spec = generate_spec(
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
        proposed_adrs=spec.design.proposed_adrs,
        session_id=payload.run_id,
    )
    logger.info(
        "spec ready",
        run_id=payload.run_id,
        spec_slug=spec.spec_slug,
        task_count=result.task_count,
    )
    return result.model_dump()


def upload_spec(spec: SpecBundle) -> None:
    """Render and upload the three spec docs to S3."""
    write_spec_doc(spec.spec_slug, "requirements", render_requirements(spec))
    write_spec_doc(spec.spec_slug, "design", render_design(spec))
    write_spec_doc(spec.spec_slug, "tasks", render_tasks(spec))


if __name__ == "__main__":
    app.run()
