"""Tests for ``common.door`` — DoorClass, DoorAssessment, classify_paths."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.door import DoorAssessment, classify_paths


def test_default_assessment_is_two_way() -> None:
    assessment = DoorAssessment()
    assert assessment.door_class == "two_way"
    assert assessment.categories == []
    assert assessment.rationale is None


def test_one_way_requires_category() -> None:
    with pytest.raises(ValidationError):
        DoorAssessment(door_class="one_way", rationale="touches prod")


def test_one_way_requires_rationale() -> None:
    with pytest.raises(ValidationError):
        DoorAssessment(door_class="one_way", categories=["production_terraform"])


def test_two_way_must_not_list_categories() -> None:
    with pytest.raises(ValidationError):
        DoorAssessment(door_class="two_way", categories=["schema_migration"])


def test_two_way_must_not_include_rationale() -> None:
    with pytest.raises(ValidationError):
        DoorAssessment(door_class="two_way", rationale="not needed")


def test_one_way_with_category_and_rationale_validates() -> None:
    assessment = DoorAssessment(
        door_class="one_way",
        categories=["production_terraform"],
        rationale="modifies terraform/envs/prod/network.tf",
    )
    assert assessment.door_class == "one_way"
    assert assessment.categories == ["production_terraform"]


def test_categories_none_coerced_to_empty_list() -> None:
    """Strands' structured_output sometimes hands back ``categories: null``."""
    assessment = DoorAssessment.model_validate({"door_class": "two_way", "categories": None})
    assert assessment.categories == []


def test_assessment_is_frozen() -> None:
    assessment = DoorAssessment()
    with pytest.raises(ValidationError):
        assessment.door_class = "one_way"  # type: ignore[misc]  # frozen=True forbids assignment


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        DoorAssessment.model_validate({"door_class": "two_way", "extra": "nope"})


def test_classify_paths_empty_input() -> None:
    assert classify_paths([]) == []


def test_classify_paths_unrelated_paths() -> None:
    paths = ["src/foo.py", "tests/test_foo.py", "README.md"]
    assert classify_paths(paths) == []


def test_classify_paths_production_terraform() -> None:
    assert classify_paths(["terraform/envs/prod/main.tf"]) == ["production_terraform"]


def test_classify_paths_schema_migration_directory() -> None:
    assert classify_paths(["migrations/0042_add_user_email.sql"]) == ["schema_migration"]


def test_classify_paths_schema_migration_filename() -> None:
    assert classify_paths(["db/init_schema.sql"]) == ["schema_migration"]


def test_classify_paths_iam_tf() -> None:
    assert classify_paths(["terraform/modules/agents/iam.tf"]) == ["iam_authorization"]


def test_classify_paths_iam_policy_json() -> None:
    assert classify_paths(["terraform/shared/policies/runner-policy.json"]) == [
        "iam_authorization",
    ]


def test_classify_paths_event_schema() -> None:
    assert classify_paths(["terraform/shared/schemas/RUN_COMPLETED.json"]) == [
        "event_schema_breaking",
    ]


def test_classify_paths_kms() -> None:
    assert classify_paths(["terraform/modules/state/kms.tf"]) == ["cryptography_or_secrets"]


def test_classify_paths_cognito() -> None:
    assert classify_paths(["terraform/modules/auth/cognito.tf"]) == ["auth_flow"]


def test_classify_paths_cron() -> None:
    assert classify_paths(["terraform/modules/improvement/schedule.tf"]) == ["scheduled_job"]


def test_classify_paths_dedupes_categories() -> None:
    paths = [
        "terraform/envs/prod/main.tf",
        "terraform/envs/prod/dashboard.tf",
        "terraform/envs/prod/agents.tf",
    ]
    assert classify_paths(paths) == ["production_terraform"]


def test_classify_paths_multiple_categories() -> None:
    paths = [
        "terraform/envs/prod/main.tf",
        "terraform/modules/agents/iam.tf",
        "terraform/shared/schemas/SPEC_READY.json",
    ]
    result = classify_paths(paths)
    assert "production_terraform" in result
    assert "iam_authorization" in result
    assert "event_schema_breaking" in result
    assert len(result) == 3


def test_classify_paths_does_not_return_content_only_categories() -> None:
    """Path classifier never returns categories that need diff-content analysis."""
    paths = ["src/api.py", "pyproject.toml", "package.json"]
    result = classify_paths(paths)
    assert "public_api_break" not in result
    assert "major_dependency_bump" not in result
    assert "public_deletion" not in result
