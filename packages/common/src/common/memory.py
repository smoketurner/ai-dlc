"""Hybrid memory: ``MEMORY.md`` (per-repo) ⊕ AgentCore Memory (cross-session).

The orchestration layer that both agents call into. ``memory_md`` and
``agentcore_memory`` are pure adapters; this module composes them with a clear
contract:

* ``MEMORY.md`` is the canonical, human-reviewed, repo-versioned memory.
* AgentCore Memory holds cross-session semantic facts and session events.
* Writes into AgentCore Memory happen exclusively from the
  ``event_projector`` Lambda (which forwards every platform event as a
  ``CreateEvent``); agents themselves never write to AgentCore Memory.

The agent's persistent filesystem is at ``/workspace`` by default; for local
runs callers pass ``fs_root=Path.cwd()`` or similar.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import boto3

from common.agentcore_memory import MemoryRecord, retrieve_memory_records
from common.errors import AgentCoreMemoryError, S3ArtifactError
from common.memory_md import MemoryDoc, parse, render
from common.s3 import get_text, put_text

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from mypy_boto3_s3.client import S3Client


_MEMORY_FILE_NAME = "MEMORY.md"
_DEFAULT_FS_ROOT = Path("/workspace")
_MEMORY_S3_KEY = "projects/{project_slug}/memory/MEMORY.md"

# Skill directories the platform recognises (agentskills.io schema).
# ``.aidlc/skills/`` lives in target repos; ``.claude/skills/`` lives in
# the ai-dlc repo itself (so the Implementer's Claude Agent SDK loop
# inherits the platform-level skill library too).
SKILL_DIRS = (".aidlc/skills", ".claude/skills")
MIN_FRONTMATTER_LINES = 2


@dataclass(frozen=True, slots=True)
class HybridMemoryConfig:
    """Identifies the AgentCore Memory + S3 instances and the active actor."""

    memory_id: str
    memory_md_bucket: str
    actor_id: str
    session_id: str

    @property
    def project_namespace(self) -> str:
        """Return the namespace prefix for project-scoped retrieval."""
        return "/projects/{project_slug}/facts"


def load_memory_md(
    *,
    project_slug: str,
    s3_client: S3Client,
    memory_md_bucket: str,
    fs_root: Path = _DEFAULT_FS_ROOT,
) -> MemoryDoc:
    """Load ``MEMORY.md`` from the persistent filesystem, falling back to S3.

    Returns an empty :class:`MemoryDoc` if neither source has the file yet.
    """
    fs_path = fs_root / _MEMORY_FILE_NAME
    if fs_path.is_file():
        return parse(fs_path.read_text(encoding="utf-8"))
    key = _MEMORY_S3_KEY.format(project_slug=project_slug)
    try:
        body = get_text(s3_client, bucket=memory_md_bucket, key=key)
    except S3ArtifactError:
        return MemoryDoc()
    return parse(body)


def save_memory_md(
    doc: MemoryDoc,
    *,
    project_slug: str,
    s3_client: S3Client,
    memory_md_bucket: str,
    fs_root: Path = _DEFAULT_FS_ROOT,
    sync_s3: bool = True,
) -> None:
    """Persist ``doc`` to the persistent filesystem and (optionally) S3.

    Args:
        doc: The document to persist.
        project_slug: Owning project slug; used as the S3 key namespace.
        s3_client: boto3 S3 client; only touched when ``sync_s3`` is true.
        memory_md_bucket: S3 bucket that holds snapshots.
        fs_root: Persistent filesystem root; defaults to ``/workspace``.
        sync_s3: When false, write only to the filesystem.
    """
    body = render(doc)
    fs_path = fs_root / _MEMORY_FILE_NAME
    fs_path.parent.mkdir(parents=True, exist_ok=True)
    fs_path.write_text(body, encoding="utf-8")
    if not sync_s3:
        return
    key = _MEMORY_S3_KEY.format(project_slug=project_slug)
    put_text(
        s3_client,
        bucket=memory_md_bucket,
        key=key,
        body=body,
        content_type="text/markdown; charset=utf-8",
    )


def retrieve_relevant_memory(
    *,
    project_slug: str,
    query: str,
    config: HybridMemoryConfig,
    memory_client: BedrockAgentCoreClient,
    top_k: int = 8,
) -> list[MemoryRecord]:
    """Search the per-project semantic namespace for facts relevant to ``query``."""
    namespace = f"/projects/{project_slug}/facts"
    return retrieve_memory_records(
        memory_client,
        memory_id=config.memory_id,
        namespace=namespace,
        query=query,
        top_k=top_k,
    )


@cache
def memory_client() -> BedrockAgentCoreClient:
    """Process-cached AgentCore Memory data-plane client."""
    return boto3.client("bedrock-agentcore")


def agent_memory_preamble(
    *,
    project_slug: str,
    query: str,
    top_k: int = 6,
    client: BedrockAgentCoreClient | None = None,
) -> str:
    """Retrieve top-K AgentCore Memory records and render them as a Markdown preamble.

    Used by every agent at invocation time to inject prior-run context into
    the user message. Best-effort — never raises:

    * Returns ``""`` when ``AIDLC_MEMORY_ID`` is unset (e.g., local dev).
    * Returns ``""`` on any retrieval error (the run continues without
      memory rather than failing on a memory-store outage).
    * Returns ``""`` when no records match.

    Otherwise returns a Markdown block ending in a horizontal rule, ready
    to be prepended to the agent's user message.
    """
    memory_id = os.environ.get("AIDLC_MEMORY_ID")
    if not memory_id:
        return ""
    bound_client = client or memory_client()
    namespace = f"/projects/{project_slug}/facts"
    try:
        records = retrieve_memory_records(
            bound_client,
            memory_id=memory_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
        )
    except AgentCoreMemoryError:
        return ""
    return render_memory_preamble(records)


def render_memory_preamble(records: list[MemoryRecord]) -> str:
    """Render retrieved records as the Markdown block agents prepend to prompts."""
    if not records:
        return ""
    lines = [
        "## Recent project context",
        "",
        "These facts about this project were extracted from prior runs by",
        "AgentCore Memory. If anything here conflicts with the current request,",
        "prefer the current request.",
        "",
    ]
    lines.extend(f"- {r.content.strip()}" for r in records if r.content.strip())
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def agent_skills_preamble(*, fs_root: Path = _DEFAULT_FS_ROOT) -> str:
    """List ``name + description`` for every packaged skill under ``fs_root``.

    Progressive-disclosure pattern (agentskills.io): the preamble shows
    only enough metadata for the agent to recognise when a skill is
    relevant; the body is loaded on demand when the agent opens the
    ``SKILL.md`` file inside the slug folder.

    Walks both ``.aidlc/skills/`` (target-repo skills) and
    ``.claude/skills/`` (platform-repo skills) for the canonical
    layout: one ``<slug>/SKILL.md`` per skill. Returns ``""`` when no
    skill files exist under ``fs_root``. Best-effort — silently skips
    files whose frontmatter doesn't parse.
    """
    repo_root = fs_root / "repo" if (fs_root / "repo").is_dir() else fs_root
    entries: list[tuple[str, str]] = []
    for relative in SKILL_DIRS:
        skill_dir = repo_root / relative
        if not skill_dir.is_dir():
            continue
        for skill_path in sorted(skill_dir.glob("*/SKILL.md")):
            meta = parse_skill_frontmatter(skill_path.read_text(encoding="utf-8"))
            if meta is None:
                continue
            entries.append(meta)
    return render_skills_preamble(entries)


def render_skills_preamble(entries: list[tuple[str, str]]) -> str:
    """Render the skills index as a Markdown block prepended to agent prompts."""
    if not entries:
        return ""
    lines = [
        "## Available skills",
        "",
        "Skills packaged from prior runs. Each is one focused procedure;",
        "the body loads on demand when you read the file.",
        "",
    ]
    lines.extend(f"- **{name}**: {description}" for name, description in entries)
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def parse_skill_frontmatter(content: str) -> tuple[str, str] | None:
    """Extract ``(name, description)`` from an agentskills.io frontmatter block.

    The block is the YAML-ish header at the top of the file delimited
    by ``---`` lines. Only ``name`` and ``description`` are required;
    everything else is ignored. Returns ``None`` when either key is
    missing or the file has no frontmatter.
    """
    if not content.startswith("---"):
        return None
    lines = content.splitlines()
    if len(lines) < MIN_FRONTMATTER_LINES:
        return None
    name = ""
    description = ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "name":
            name = value
        elif key == "description":
            description = value
    if not name or not description:
        return None
    return name, description
