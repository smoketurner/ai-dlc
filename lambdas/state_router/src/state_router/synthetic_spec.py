"""Markdown rendering for the synthetic-spec workflows.

Triage classifies ``bug_fix`` / ``upgrade`` / ``docs`` requests as
"don't need a full architect spec" and the router synthesizes a
single-task spec inline from the run's intent. The three docs are
the minimum the implementer expects: requirements, design, tasks.

Pure templates — no AWS calls, no DDB. The router uploads the
returned strings to S3 in :func:`state_router.execute.execute_write_synthetic_spec`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from state_router.model import Run

SYNTHETIC_TASK_ID = "T-001"
"""Single task id used by the synthetic-spec (bug_fix / upgrade / docs) flow."""


def render_requirements(run: Run) -> str:
    """Render a single-task requirements doc from the run's intent."""
    return (
        f"# Requirements\n\n"
        f"## Source\n\n"
        f"Auto-synthesized for `{run.workflow_kind}` workflow from "
        f"{run.source_issue_url or 'programmatic request'}.\n\n"
        f"## Intent\n\n{run.intent}\n"
    )


def render_design(run: Run) -> str:
    """Render a minimal design doc that defers the design decisions to the implementer."""
    return (
        f"# Design\n\n"
        f"Workflow: `{run.workflow_kind}`. The implementer is expected to read the "
        f"target repo, understand existing structure, and make the smallest viable change.\n"
    )


def render_tasks(run: Run) -> str:
    """Render a one-task tasks doc keyed on :data:`SYNTHETIC_TASK_ID`."""
    return (
        f"# Tasks\n\n"
        f"## {SYNTHETIC_TASK_ID} — {run.workflow_kind}: address the request\n\n"
        f"Implement the change described in the requirements. Open one PR.\n"
    )
