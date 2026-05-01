"""Hybrid memory: ``MEMORY.md`` (per-repo) ⊕ AgentCore Memory (cross-session).

The orchestration layer that both agents call into. ``memory_md`` and
``agentcore_memory`` are pure adapters; this module composes them with a clear
contract:

* ``MEMORY.md`` is the canonical, human-reviewed, repo-versioned memory.
* AgentCore Memory holds cross-session semantic facts and session events.
* Sync direction is one-way at write time: a session's diff to ``MEMORY.md``
  is replayed into AgentCore Memory as a ``CreateEvent``. The reverse
  direction (long-term records flowing into ``MEMORY.md``) is mediated by
  the agent proposing edits in its PR — humans gate the actual write.

The agent's persistent filesystem is at ``/workspace`` by default; for local
runs callers pass ``fs_root=Path.cwd()`` or similar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from common.agentcore_memory import (
    MemoryEvent,
    MemoryRecord,
    create_event,
    retrieve_memory_records,
)
from common.errors import S3ArtifactError
from common.memory_md import MemoryDoc, parse, render
from common.s3 import get_text, put_text

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from mypy_boto3_s3.client import S3Client


_MEMORY_FILE_NAME = "MEMORY.md"
_DEFAULT_FS_ROOT = Path("/workspace")
_MEMORY_S3_KEY = "projects/{project_slug}/memory/MEMORY.md"


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


def sync_to_agentcore(
    diff_text: str,
    *,
    project_slug: str,
    config: HybridMemoryConfig,
    memory_client: BedrockAgentCoreClient,
) -> str | None:
    """Replay a ``MEMORY.md`` diff into AgentCore Memory as a single event.

    Args:
        diff_text: Markdown-formatted diff of the just-saved file. When empty
            (no changes), this function returns ``None`` without making a call.
        project_slug: Owning project slug; embedded in the namespace.
        config: Hybrid memory config.
        memory_client: AgentCore Memory data-plane client.

    Returns:
        The new event id, or ``None`` if there was nothing to sync.
    """
    if not diff_text.strip():
        return None
    return create_event(
        memory_client,
        memory_id=config.memory_id,
        actor_id=f"{config.actor_id}@{project_slug}",
        session_id=config.session_id,
        events=[MemoryEvent(role="ASSISTANT", text=diff_text)],
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
