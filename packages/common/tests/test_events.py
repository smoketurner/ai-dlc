"""Tests for ``common.events``."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from common.events import EventEnvelope, RequestReceived, RunCompleted
from common.ids import new_correlation_id, new_event_id, new_run_id


def _env(payload: RequestReceived) -> EventEnvelope[RequestReceived]:
    return EventEnvelope[RequestReceived](
        type="REQUEST.RECEIVED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="test",
        payload=payload,
    )


def test_round_trip_request_received() -> None:
    payload = RequestReceived(project_slug="demo", intent="add /healthz", requestor="alice")
    env = _env(payload)
    raw = env.model_dump_json()
    parsed = EventEnvelope[RequestReceived].model_validate_json(raw)
    assert parsed == env
    assert parsed.payload.project_slug == "demo"


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        RequestReceived.model_validate(
            {
                "project_slug": "demo",
                "intent": "x",
                "requestor": "alice",
                "extra_field": "should fail",
            },
        )


def test_run_completed_payload_has_required_fields() -> None:
    payload = RunCompleted(
        project_slug="demo",
        total_duration_ms=12345,
        total_token_in=4096,
        total_token_out=2048,
        total_cost_usd=0.42,
    )
    rendered = json.loads(payload.model_dump_json())
    assert rendered["total_cost_usd"] == 0.42


def test_envelope_type_is_literal_pinned() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    with pytest.raises(ValidationError):
        EventEnvelope[RequestReceived](
            type="NOT.A.REAL.TYPE",  # ty: ignore[invalid-argument-type]
            run_id=new_run_id(),
            correlation_id=new_correlation_id(),
            actor_id="t",
            payload=payload,
        )


def test_event_id_default_is_unique() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    a = _env(payload)
    b = _env(payload)
    assert a.event_id != b.event_id


def test_causation_id_optional() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    env = _env(payload).model_copy(update={"causation_id": new_event_id()})
    assert env.causation_id is not None
