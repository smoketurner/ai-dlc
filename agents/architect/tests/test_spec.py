"""Tests for architect.spec — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from architect.spec import (
    AcceptanceCriterion,
    Design,
    DesignComponent,
    Requirements,
    SpecBundle,
    Task,
    UserStory,
    render_design,
    render_requirements,
    render_tasks,
)


def make_spec(*, with_optional: bool = False) -> SpecBundle:
    """Build a minimal valid spec; toggles optional fields for richer renders."""
    requirements = Requirements(
        summary="Add a /healthz endpoint to every public-facing service.",
        user_stories=[
            UserStory(
                id="R-001",
                role="oncall engineer",
                capability="hit /healthz on any service",
                outcome="I can confirm liveness without authenticating",
            ),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-R-001-a",
                requirement_id="R-001",
                given="the dashboard is running",
                when="I GET /healthz",
                then="the response is 200 with body {ok: true}",
            ),
        ],
        out_of_scope=["readiness checks"] if with_optional else [],
        open_questions=["should we include build SHA in the body?"] if with_optional else [],
    )
    design = Design(
        approach="One FastAPI route per service. No middleware.",
        components=[
            DesignComponent(
                name="healthz route",
                purpose="returns liveness JSON",
                location="services/dashboard/src/dashboard/routes/health.py",
            ),
        ],
        data_model="HealthOk { ok: bool, build_sha: str }",
        sequence="1. GET /healthz\n2. return HealthOk",
        failure_modes=["panic in middleware blocks the route"] if with_optional else [],
        trade_offs=["chose response model over plain dict for schema"] if with_optional else [],
        proposed_adrs=["docs/ADRs/0007-healthz.md — health-check contract"]
        if with_optional
        else [],
        references=["requirements R-001"] if with_optional else [],
    )
    tasks = [
        Task(
            id="T-001",
            title="Add /healthz route",
            implements=["AC-R-001-a"],
            touches=["services/dashboard/src/dashboard/routes/health.py"] if with_optional else [],
            done_when="curl /healthz returns 200 {ok: true}",
        ),
    ]
    return SpecBundle(
        spec_slug="add-healthz",
        feature_name="Add /healthz endpoint",
        requirements=requirements,
        design=design,
        tasks=tasks,
    )


def test_minimal_spec_validates() -> None:
    spec = make_spec()
    assert spec.spec_slug == "add-healthz"
    assert len(spec.tasks) == 1


def test_invalid_slug_rejected() -> None:
    with pytest.raises(ValidationError):
        SpecBundle(
            spec_slug="Bad Slug!",
            feature_name="x",
            requirements=make_spec().requirements,
            design=make_spec().design,
            tasks=make_spec().tasks,
        )


def test_invalid_task_id_rejected() -> None:
    with pytest.raises(ValidationError):
        Task(
            id="task1",
            title="x",
            implements=["AC-R-001-a"],
            done_when="x",
        )


def test_invalid_requirement_id_rejected() -> None:
    with pytest.raises(ValidationError):
        UserStory(id="REQ-1", role="x", capability="x", outcome="x")


def test_render_requirements_includes_user_story() -> None:
    out = render_requirements(make_spec())
    assert "# Requirements — Add /healthz endpoint" in out
    assert "**R-001** — As a oncall engineer" in out
    assert "**AC-R-001-a** (R-001)" in out


def test_render_requirements_optional_sections_skipped_when_empty() -> None:
    out = render_requirements(make_spec(with_optional=False))
    assert "## Out of scope" not in out
    assert "## Open questions" not in out


def test_render_requirements_optional_sections_present_when_filled() -> None:
    out = render_requirements(make_spec(with_optional=True))
    assert "## Out of scope" in out
    assert "## Open questions" in out


def test_render_design_includes_components_and_blocks() -> None:
    out = render_design(make_spec(with_optional=True))
    assert "**healthz route**" in out
    assert "```text" in out
    assert "## ADRs proposed" in out


def test_render_tasks_emits_checklist() -> None:
    out = render_tasks(make_spec(with_optional=True))
    assert "- [ ] **T-001** — Add /healthz route" in out
    assert "**Implements:** AC-R-001-a" in out
    assert "**Touches:** `services/dashboard/src/dashboard/routes/health.py`" in out
    assert out.endswith("\n")


def test_spec_is_frozen() -> None:
    spec = make_spec()
    with pytest.raises(ValidationError):
        spec.spec_slug = "different"  # type: ignore[misc]  # frozen=True forbids assignment
