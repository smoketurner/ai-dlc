"""Tests for ``common.memory_md``."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from common import memory_md
from common.errors import MemoryDocParseError
from common.memory_md import (
    MemoryDoc,
    parse,
    read_stack_profile,
    render,
    stack_profile_key,
    write_stack_profile,
)
from common.stack_discovery import StackComponent, StackProfile

_MEMORY_BUCKET = "ai-dlc-test-memory-md"
_PROJECT_SLUG = "ai-dlc"

_MINIMAL = """# Project Memory

Intro paragraph.

## Overview

ai-dlc is the agentic SDLC platform.

## Conventions

- Python 3.14.

## Decisions

- ADR-0001: use uv.

## Constraints

- Python 3.14 minimum.

## Glossary

- ADR — Architectural Decision Record.

## Notes

(none)
"""


def test_parse_minimal_valid() -> None:
    doc = parse(_MINIMAL)
    assert doc.title == "Project Memory"
    assert doc.intro == "Intro paragraph."
    assert doc.sections["overview"].startswith("ai-dlc")
    assert "ADR-0001" in doc.sections["decisions"]
    assert "Python 3.14 minimum" in doc.sections["constraints"]


def test_render_round_trip_is_stable() -> None:
    doc = parse(_MINIMAL)
    rendered = render(doc)
    again = parse(rendered)
    assert doc == again


def test_unknown_section_fails_fast() -> None:
    bad = _MINIMAL + "\n## SecretLore\n\nshould not be allowed\n"
    with pytest.raises(MemoryDocParseError):
        parse(bad)


def test_out_of_order_sections_fail() -> None:
    swapped = _MINIMAL.replace("## Overview", "## Conventions").replace(
        "## Conventions\n\n- Python 3.14.",
        "## Overview\n\nai-dlc is the agentic SDLC platform.",
    )
    with pytest.raises(MemoryDocParseError):
        parse(swapped)


def test_with_section_replaces() -> None:
    doc = parse(_MINIMAL)
    updated = doc.with_section("notes", "Newly added note.")
    assert updated.sections["notes"] == "Newly added note."
    # Other sections are untouched.
    assert updated.sections["overview"] == doc.sections["overview"]


def test_with_appended_preserves_existing() -> None:
    doc = parse(_MINIMAL)
    updated = doc.with_appended("decisions", "- ADR-0002: choose ECS over App Runner.")
    assert "ADR-0001: use uv." in updated.sections["decisions"]
    assert "ADR-0002" in updated.sections["decisions"]


def test_empty_doc_renders_all_six_headers() -> None:
    rendered = render(MemoryDoc())
    for header in (
        "## Overview",
        "## Conventions",
        "## Decisions",
        "## Constraints",
        "## Glossary",
        "## Notes",
    ):
        assert header in rendered


# ---------------------------------------------------------------------------
# read_stack_profile / write_stack_profile — JSON snapshot of stack discovery
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """moto-backed S3 + bucket created + boto3 client cache cleared."""
    monkeypatch.setenv("AIDLC_MEMORY_MD_BUCKET", _MEMORY_BUCKET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    memory_md.memory_md_s3_client.cache_clear()
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=_MEMORY_BUCKET)
        yield
    memory_md.memory_md_s3_client.cache_clear()


_SAMPLE_PROFILE = StackProfile(
    components=(
        StackComponent(
            path=".",
            language="python",
            version=">=3.14,<3.15",
            package_manager="uv",
            manifest="pyproject.toml",
            test_command="uv run pytest",
        ),
    ),
    primary_language="python",
    workspace_kind="uv",
    monorepo=True,
    polyglot=False,
    containerized=True,
    ci_provider="github-actions",
    tool_versions={"python": "3.14.0"},
)


def test_stack_profile_key_uses_per_project_namespace() -> None:
    assert stack_profile_key("ai-dlc") == "projects/ai-dlc/stack_profile.json"


def test_read_stack_profile_returns_none_when_missing(memory_bucket: None) -> None:
    del memory_bucket
    assert read_stack_profile(_PROJECT_SLUG) is None


def test_write_then_read_roundtrip(memory_bucket: None) -> None:
    del memory_bucket
    assert write_stack_profile(_PROJECT_SLUG, _SAMPLE_PROFILE) is True
    loaded = read_stack_profile(_PROJECT_SLUG)
    assert loaded == _SAMPLE_PROFILE


def test_write_is_idempotent(memory_bucket: None) -> None:
    """Second write of identical content is skipped (returns False)."""
    del memory_bucket
    assert write_stack_profile(_PROJECT_SLUG, _SAMPLE_PROFILE) is True
    assert write_stack_profile(_PROJECT_SLUG, _SAMPLE_PROFILE) is False


def test_write_re_puts_on_content_change(memory_bucket: None) -> None:
    del memory_bucket
    write_stack_profile(_PROJECT_SLUG, _SAMPLE_PROFILE)
    updated = _SAMPLE_PROFILE.model_copy(update={"polyglot": True})
    assert write_stack_profile(_PROJECT_SLUG, updated) is True
    loaded = read_stack_profile(_PROJECT_SLUG)
    assert loaded is not None
    assert loaded.polyglot is True


def test_read_stack_profile_returns_none_for_invalid_json(memory_bucket: None) -> None:
    """An old or hand-edited snapshot that no longer parses → ``None``."""
    del memory_bucket
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=_MEMORY_BUCKET,
        Key=stack_profile_key(_PROJECT_SLUG),
        Body=b'{"components": [], "unexpected_field": 1}',
        ContentType="application/json",
    )
    assert read_stack_profile(_PROJECT_SLUG) is None
