"""Parser, renderer, and S3-snapshot reader for the ai-dlc ``MEMORY.md``.

A MEMORY.md has exactly six top-level ``## `` headers, in this order:

1. Overview
2. Conventions
3. Decisions
4. Constraints
5. Glossary
6. Notes

Anything else fails fast with :class:`MemoryDocParseError`. The strict
schema is intentional — agents need to write to known sections without an
LLM negotiating the structure on every save.

This module also owns :func:`read_memory_md` — the canonical read of
the per-project MEMORY.md S3 snapshot the architect syncs from the
cloned repo. Each Strands agent imports and registers it directly so
the read semantics (freshness header, empty-on-missing) live in one
place rather than four near-identical copies.
"""

from __future__ import annotations

import os
import re
from functools import cache
from typing import TYPE_CHECKING, Final, Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field

from common.errors import MemoryDocParseError
from common.stack_discovery import StackProfile, render_stack_profile

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

Section = Literal[
    "overview",
    "conventions",
    "decisions",
    "constraints",
    "glossary",
    "notes",
]

_SECTION_ORDER: Final[tuple[Section, ...]] = (
    "overview",
    "conventions",
    "decisions",
    "constraints",
    "glossary",
    "notes",
)
_SECTION_TITLES: Final[dict[Section, str]] = {
    "overview": "Overview",
    "conventions": "Conventions",
    "decisions": "Decisions",
    "constraints": "Constraints",
    "glossary": "Glossary",
    "notes": "Notes",
}
_TITLE_TO_SECTION: Final[dict[str, Section]] = {v: k for k, v in _SECTION_TITLES.items()}

_HEADER_RE: Final = re.compile(r"^##\s+(?P<title>.+?)\s*$")


class MemoryDoc(BaseModel):
    """In-memory representation of a ``MEMORY.md`` file."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    title: str = "Project Memory"
    intro: str = ""
    sections: dict[Section, str] = Field(
        default_factory=lambda: dict.fromkeys(_SECTION_ORDER, ""),
    )

    def with_section(self, section: Section, content: str) -> MemoryDoc:
        """Return a copy with ``section`` set to ``content``."""
        new_sections = dict(self.sections)
        new_sections[section] = content
        return self.model_copy(update={"sections": new_sections})

    def with_appended(self, section: Section, content: str) -> MemoryDoc:
        """Return a copy with ``content`` appended to ``section``."""
        existing = self.sections.get(section, "")
        joined = f"{existing.rstrip()}\n{content.lstrip()}" if existing else content
        return self.with_section(section, joined.strip())


def parse(text: str) -> MemoryDoc:
    """Parse a ``MEMORY.md`` file body into a :class:`MemoryDoc`.

    Raises:
        MemoryDocParseError: On unknown headers, missing sections, or
            out-of-order sections.
    """
    lines = text.splitlines()
    title, intro, body_start = _extract_title_and_intro(lines)
    sections, seen_order = _extract_sections(lines, body_start)
    _validate_section_order(seen_order)
    full_sections: dict[Section, str] = {key: sections.get(key, "") for key in _SECTION_ORDER}
    return MemoryDoc(title=title, intro=intro, sections=full_sections)


def render(doc: MemoryDoc) -> str:
    """Render a :class:`MemoryDoc` back to canonical ``MEMORY.md`` markdown."""
    parts: list[str] = [f"# {doc.title}", ""]
    if doc.intro.strip():
        parts.extend([doc.intro.strip(), ""])
    for key in _SECTION_ORDER:
        parts.append(f"## {_SECTION_TITLES[key]}")
        body = doc.sections.get(key, "").strip()
        parts.extend(["", body, ""] if body else ["", ""])
    return "\n".join(parts).rstrip() + "\n"


def _extract_title_and_intro(lines: list[str]) -> tuple[str, str, int]:
    """Return ``(title, intro, index_of_first_##_or_eof)``."""
    title = "Project Memory"
    intro_lines: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].startswith("##"):
        line = lines[i]
        if line.startswith("# ") and title == "Project Memory":
            title = line[2:].strip()
        elif line.startswith("# "):
            raise MemoryDocParseError("multiple top-level # titles", line=line)
        else:
            intro_lines.append(line)
        i += 1
    return title, "\n".join(intro_lines).strip(), i


def _extract_sections(lines: list[str], start: int) -> tuple[dict[Section, str], list[Section]]:
    """Return ``(section -> body, encountered_order)``."""
    sections: dict[Section, str] = {}
    encountered: list[Section] = []
    current: Section | None = None
    buffer: list[str] = []
    for line in lines[start:]:
        match = _HEADER_RE.match(line) if line.startswith("## ") else None
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            title = match.group("title")
            if title not in _TITLE_TO_SECTION:
                raise MemoryDocParseError("unknown section header", title=title)
            current = _TITLE_TO_SECTION[title]
            encountered.append(current)
            buffer = []
        else:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections, encountered


def _validate_section_order(seen: list[Section]) -> None:
    """Raise if sections are duplicated or out of canonical order."""
    if len(seen) != len(set(seen)):
        raise MemoryDocParseError("duplicate section header", seen=seen)
    if [s for s in _SECTION_ORDER if s in seen] != seen:
        raise MemoryDocParseError(
            "section headers out of canonical order",
            expected=list(_SECTION_ORDER),
            seen=seen,
        )


@cache
def memory_md_s3_client() -> S3Client:
    """Process-cached S3 client used to read the per-project snapshot."""
    return boto3.client("s3")


def memory_md_bucket() -> str:
    """Bucket name for the per-project MEMORY.md snapshot."""
    return os.environ["AIDLC_MEMORY_MD_BUCKET"]


def read_memory_md(project_slug: str) -> str:
    """Read the canonical MEMORY.md for a project, prefixed with sync time.

    The architect syncs the project's ``MEMORY.md`` (root, or
    ``docs/MEMORY.md`` for legacy repos) + ``AGENTS.md`` from the
    cloned repo into ``s3://{bucket}/projects/{project_slug}/MEMORY.md``
    on every architect run (see
    ``architect.repo_grounding.sync_memory_md_from_clone``). The four
    Strands agents (architect, critic, reviewer, proposer) register
    this function directly as a tool — no per-agent wrapper.

    The freshness header is injected at read time rather than baked
    into the stored body so two syncs of identical source content
    produce a byte-identical S3 object — that's what keeps the
    architect's MD5 idempotency check valid.

    Args:
        project_slug: Project identifier — e.g., ``ai-dlc``.

    Returns:
        The Markdown body prefixed with ``_(synced from clone on
        <iso>)_``, or the empty string when no snapshot exists for
        the project.
    """
    key = f"projects/{project_slug}/MEMORY.md"
    try:
        obj = memory_md_s3_client().get_object(Bucket=memory_md_bucket(), Key=key)
    except Exception:
        return ""
    body = obj["Body"].read().decode("utf-8")
    last_modified = obj.get("LastModified")
    if last_modified is None:
        return body
    return f"_(synced from clone on {last_modified.isoformat()})_\n\n{body}"


STACK_PROFILE_KEY_TEMPLATE: Final = "projects/{project_slug}/stack_profile.json"


def stack_profile_key(project_slug: str) -> str:
    """Return the S3 key under which the project's stack profile lives."""
    return STACK_PROFILE_KEY_TEMPLATE.format(project_slug=project_slug)


def read_stack_profile(project_slug: str) -> StackProfile | None:
    """Read the per-project :class:`StackProfile` snapshot from S3.

    Returns ``None`` when no snapshot has been written for the project
    yet, or when the stored JSON fails to validate against the current
    :class:`StackProfile` schema (which means the snapshot is from an
    older code version and should be regenerated).

    Args:
        project_slug: Project identifier — e.g., ``ai-dlc``.
    """
    key = stack_profile_key(project_slug)
    try:
        obj = memory_md_s3_client().get_object(Bucket=memory_md_bucket(), Key=key)
    except BotoCoreError, ClientError:
        return None
    raw = obj["Body"].read().decode("utf-8")
    try:
        return StackProfile.model_validate_json(raw)
    except ValueError:
        return None


def write_stack_profile(project_slug: str, profile: StackProfile) -> bool:
    """Persist the project's :class:`StackProfile` to S3.

    Idempotent: when the rendered JSON matches what's already at the key,
    the write is skipped. Returns ``True`` when a put was issued.

    Args:
        project_slug: Project identifier — e.g., ``ai-dlc``.
        profile: The profile to persist.
    """
    body = profile.model_dump_json(indent=2)
    key = stack_profile_key(project_slug)
    bucket = memory_md_bucket()
    client = memory_md_s3_client()
    if stack_profile_unchanged(client, bucket, key, body):
        return False
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return True


def stack_profile_unchanged(client: S3Client, bucket: str, key: str, body: str) -> bool:
    """Return True when the stored object's body equals ``body`` byte-for-byte."""
    try:
        existing = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except BotoCoreError, ClientError:
        return False
    return existing == body


def read_stack_profile_md(project_slug: str) -> str:
    """Read the project's stack profile and render it as Markdown for an agent.

    Companion to :func:`read_memory_md`. Each Strands agent registers
    this function directly as a tool so the rendering semantics
    (compact, component-grouped, ``<stack_profile>``-style block) live
    in one place rather than four near-identical copies.

    Args:
        project_slug: Project identifier — e.g., ``ai-dlc``.

    Returns:
        Rendered Markdown, or the empty string when no profile has been
        written yet for the project (e.g., the architect hasn't run, or
        the run had no ``target_repo``). Callers treat ``""`` as "no
        stack signal" and fall back to manual file inspection via
        ``list_repo_paths`` / ``read_repo_file``.
    """
    profile = read_stack_profile(project_slug)
    if profile is None:
        return ""
    return render_stack_profile(profile)
