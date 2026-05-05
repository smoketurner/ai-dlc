"""E2E smoke test: spec materialization pipeline.

Loads a pre-canned SpecBundle fixture, validates it against the schema,
materializes the three Markdown docs into a temp directory, then asserts
structure and content — no LLM calls, no AWS calls.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from architect.spec import SpecBundle, render_design, render_requirements, render_tasks

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "smoke_spec_bundle.json"
SPEC_SLUG = "smoke-test-fixture"


def _materialize(spec: SpecBundle, base: pathlib.Path) -> pathlib.Path:
    """Render spec docs and write them under base/docs/specs/{spec_slug}/."""
    out = base / "docs" / "specs" / spec.spec_slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "requirements.md").write_text(render_requirements(spec), encoding="utf-8")
    (out / "design.md").write_text(render_design(spec), encoding="utf-8")
    (out / "tasks.md").write_text(render_tasks(spec), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fixture_validates_against_spec_bundle_schema() -> None:
    """AC-001: fixture JSON parses and validates as a SpecBundle."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bundle = SpecBundle.model_validate(raw)
    assert bundle.spec_slug == SPEC_SLUG
    assert len(bundle.requirements.user_stories) >= 1
    assert len(bundle.requirements.acceptance_criteria) >= 1
    assert len(bundle.design.components) >= 1
    assert len(bundle.tasks) >= 1


def test_materialization_creates_all_three_files(tmp_path: pathlib.Path) -> None:
    """AC-002: all three Markdown files are created under the expected path."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bundle = SpecBundle.model_validate(raw)
    out = _materialize(bundle, tmp_path)
    assert (out / "requirements.md").exists()
    assert (out / "design.md").exists()
    assert (out / "tasks.md").exists()


def test_materialized_files_are_non_empty(tmp_path: pathlib.Path) -> None:
    """AC-002: each file has non-zero byte content."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bundle = SpecBundle.model_validate(raw)
    out = _materialize(bundle, tmp_path)
    for name in ("requirements.md", "design.md", "tasks.md"):
        assert (out / name).stat().st_size > 0, f"{name} is empty"


@pytest.mark.parametrize(
    ("filename", "header"),
    [
        ("requirements.md", "## User stories"),
        ("requirements.md", "## Acceptance criteria"),
        ("design.md", "## Components"),
        ("design.md", "## Approach"),
        ("tasks.md", "- [ ] **T-001**"),
    ],
)
def test_materialized_files_contain_expected_headers(
    tmp_path: pathlib.Path,
    filename: str,
    header: str,
) -> None:
    """AC-003: expected section headers are present in each rendered file."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bundle = SpecBundle.model_validate(raw)
    out = _materialize(bundle, tmp_path)
    content = (out / filename).read_text(encoding="utf-8")
    assert header in content, f"'{header}' not found in {filename}"
