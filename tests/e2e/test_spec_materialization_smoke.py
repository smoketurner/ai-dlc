"""E2E smoke test for the spec materialization pipeline.

Validates that a SpecBundle fixture can be:
  1. Loaded and validated against the SpecBundle schema.
  2. Materialized into requirements.md, design.md, and tasks.md.
  3. Produces non-empty files with expected section headers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from architect.spec import SpecBundle, render_design, render_requirements, render_tasks

_FIXTURE = Path(__file__).parent / "fixtures" / "smoke_spec_bundle.json"


def materialize(spec: SpecBundle, output_dir: Path) -> None:
    """Write the three spec Markdown files into output_dir/docs/specs/{slug}/."""
    dest = output_dir / "docs" / "specs" / spec.spec_slug
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "requirements.md").write_text(render_requirements(spec), encoding="utf-8")
    (dest / "design.md").write_text(render_design(spec), encoding="utf-8")
    (dest / "tasks.md").write_text(render_tasks(spec), encoding="utf-8")


@pytest.fixture(scope="module")
def spec_bundle() -> SpecBundle:
    """Load and validate the pre-canned SpecBundle fixture."""
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return SpecBundle.model_validate(raw)


def test_fixture_validates_against_schema(spec_bundle: SpecBundle) -> None:
    assert spec_bundle.spec_slug == "smoke-test-fixture"
    assert spec_bundle.feature_name
    assert len(spec_bundle.requirements.user_stories) >= 1
    assert len(spec_bundle.requirements.acceptance_criteria) >= 1
    assert len(spec_bundle.design.components) >= 1
    assert len(spec_bundle.tasks) >= 1


def test_materialization_produces_three_files(spec_bundle: SpecBundle, tmp_path: Path) -> None:
    materialize(spec_bundle, tmp_path)
    base = tmp_path / "docs" / "specs" / spec_bundle.spec_slug
    assert (base / "requirements.md").exists()
    assert (base / "design.md").exists()
    assert (base / "tasks.md").exists()


def test_materialized_files_are_non_empty(spec_bundle: SpecBundle, tmp_path: Path) -> None:
    materialize(spec_bundle, tmp_path)
    base = tmp_path / "docs" / "specs" / spec_bundle.spec_slug
    for name in ("requirements.md", "design.md", "tasks.md"):
        content = (base / name).read_text(encoding="utf-8")
        assert content.strip(), f"{name} must not be empty"


def test_requirements_md_has_expected_headers(spec_bundle: SpecBundle, tmp_path: Path) -> None:
    materialize(spec_bundle, tmp_path)
    content = (tmp_path / "docs" / "specs" / spec_bundle.spec_slug / "requirements.md").read_text(
        encoding="utf-8"
    )
    assert "## User stories" in content
    assert "## Acceptance criteria" in content


def test_design_md_has_expected_headers(spec_bundle: SpecBundle, tmp_path: Path) -> None:
    materialize(spec_bundle, tmp_path)
    content = (tmp_path / "docs" / "specs" / spec_bundle.spec_slug / "design.md").read_text(
        encoding="utf-8"
    )
    assert "## Components" in content
    assert "## Approach" in content


def test_tasks_md_has_expected_headers(spec_bundle: SpecBundle, tmp_path: Path) -> None:
    materialize(spec_bundle, tmp_path)
    content = (tmp_path / "docs" / "specs" / spec_bundle.spec_slug / "tasks.md").read_text(
        encoding="utf-8"
    )
    assert "## Tasks" in content or "# Tasks" in content
