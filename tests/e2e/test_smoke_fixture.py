"""Unit test: smoke_spec_bundle.json passes SpecBundle schema validation."""

import json
from pathlib import Path

from architect.spec import SpecBundle

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "smoke_spec_bundle.json"


def test_smoke_spec_bundle_fixture_is_valid() -> None:
    """Fixture JSON parses into a valid SpecBundle without error."""
    raw = json.loads(FIXTURE_PATH.read_text())
    bundle = SpecBundle.model_validate(raw)
    assert bundle.spec_slug == "smoke-test-fixture"
    assert len(bundle.tasks) >= 1
    assert len(bundle.requirements.user_stories) >= 1
    assert len(bundle.requirements.acceptance_criteria) >= 1
    assert len(bundle.design.components) >= 1
