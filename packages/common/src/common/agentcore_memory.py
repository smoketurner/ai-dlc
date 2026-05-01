"""Thin wrapper around AgentCore Memory's data-plane API.

The full SDK surface (``bedrock-agentcore``) is large. We only use the calls
below in the initial slice; the wrapper keeps argument names consistent with
the rest of the codebase and converts errors into :class:`AgentCoreMemoryError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import BotoCoreError, ClientError

from common.errors import AgentCoreMemoryError

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from mypy_boto3_bedrock_agentcore.type_defs import PayloadTypeTypeDef


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    """A short-term event written via ``CreateEvent``."""

    role: str  # USER | ASSISTANT | TOOL
    text: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A long-term record returned by ``RetrieveMemoryRecords``."""

    record_id: str
    namespace: str
    content: str
    score: float


def create_event(
    client: BedrockAgentCoreClient,
    /,
    *,
    memory_id: str,
    actor_id: str,
    session_id: str,
    events: list[MemoryEvent],
    timestamp: datetime | None = None,
) -> str:
    """Append a batch of events to short-term memory.

    Args:
        client: AgentCore Memory data-plane client.
        memory_id: Resource id of the memory.
        actor_id: Stable identity for the writer (user or agent name).
        session_id: AgentCore Runtime session id.
        events: Ordered events to append.
        timestamp: Event timestamp; defaults to now (UTC).

    Returns:
        The newly created event id.
    """
    payload: Sequence[PayloadTypeTypeDef] = cast(
        "Sequence[PayloadTypeTypeDef]",
        [{"conversational": {"role": e.role, "content": {"text": e.text}}} for e in events],
    )
    try:
        response = client.create_event(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=timestamp or datetime.now(UTC),
            payload=payload,
        )
    except (BotoCoreError, ClientError) as exc:
        raise AgentCoreMemoryError(
            "create_event failed",
            memory_id=memory_id,
            session_id=session_id,
        ) from exc
    event = response.get("event")
    if not isinstance(event, dict):
        raise AgentCoreMemoryError("missing event in response", memory_id=memory_id)
    event_id = event.get("eventId")
    if not isinstance(event_id, str):
        raise AgentCoreMemoryError("missing eventId in response", memory_id=memory_id)
    return event_id


def retrieve_memory_records(
    client: BedrockAgentCoreClient,
    /,
    *,
    memory_id: str,
    namespace: str,
    query: str,
    top_k: int = 8,
) -> list[MemoryRecord]:
    """Semantic-search the long-term store.

    Args:
        client: AgentCore Memory data-plane client.
        memory_id: AgentCore Memory resource id.
        namespace: Hierarchical namespace (e.g., ``/projects/{slug}/facts``).
        query: Natural-language query.
        top_k: Maximum results.
    """
    try:
        response = client.retrieve_memory_records(
            memoryId=memory_id,
            namespace=namespace,
            searchCriteria={"searchQuery": query, "topK": top_k},
        )
    except (BotoCoreError, ClientError) as exc:
        raise AgentCoreMemoryError(
            "retrieve_memory_records failed",
            memory_id=memory_id,
            namespace=namespace,
        ) from exc
    raw = response.get("memoryRecordSummaries", [])
    return [_parse_record(entry, namespace) for entry in raw]


def _parse_record(entry: Mapping[str, Any], default_namespace: str) -> MemoryRecord:
    """Coerce a raw SDK record dict into a typed :class:`MemoryRecord`."""
    content_field = entry.get("content")
    content_text = content_field.get("text", "") if isinstance(content_field, dict) else ""
    score_raw = entry.get("score", 0.0)
    score = float(score_raw) if isinstance(score_raw, int | float) else 0.0
    return MemoryRecord(
        record_id=str(entry.get("memoryRecordId", "")),
        namespace=str(entry.get("namespace", default_namespace)),
        content=str(content_text),
        score=score,
    )
