"""Tests for ``common.ids``."""

from __future__ import annotations

import re

from common.ids import (
    event_id_for_delivery,
    new_approval_id,
    new_correlation_id,
    new_event_id,
    new_run_id,
    new_session_id,
)

_UUID5_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
)

_UUID7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
)


def test_new_run_id_is_uuid7() -> None:
    rid = new_run_id()
    assert _UUID7_RE.match(rid), f"not a UUID7: {rid}"


def test_event_ids_are_unique() -> None:
    ids = {new_event_id() for _ in range(100)}
    assert len(ids) == 100


def test_session_id_is_deterministic_format() -> None:
    rid = new_run_id()
    sid = new_session_id(agent_name="architect", run_id=rid)
    assert sid == f"{rid}-architect"


def test_event_ids_are_chronologically_sortable() -> None:
    a = new_event_id()
    b = new_event_id()
    # UUID7 first 48 bits encode unix-ms; later issuance must sort >= earlier.
    assert b >= a


def test_distinct_id_kinds_unique() -> None:
    samples = {new_run_id(), new_event_id(), new_correlation_id(), new_approval_id()}
    assert len(samples) == 4


def test_event_id_for_delivery_is_uuid5() -> None:
    eid = event_id_for_delivery(delivery_id="dlv-1", event_type="CHECKS.FAILED")
    assert _UUID5_RE.match(eid), f"not a UUID5: {eid}"


def test_event_id_for_delivery_is_deterministic() -> None:
    """Same ``(delivery_id, event_type)`` tuple → same event id every time."""
    a = event_id_for_delivery(delivery_id="dlv-42", event_type="CHECKS.FAILED")
    b = event_id_for_delivery(delivery_id="dlv-42", event_type="CHECKS.FAILED")
    assert a == b


def test_event_id_for_delivery_distinguishes_event_type() -> None:
    """One redelivery that fans out to two event types must not collide."""
    a = event_id_for_delivery(delivery_id="dlv-42", event_type="CHECKS.FAILED")
    b = event_id_for_delivery(delivery_id="dlv-42", event_type="CHECKS.PASSED")
    assert a != b


def test_event_id_for_delivery_distinguishes_delivery_id() -> None:
    """Distinct webhook deliveries must produce distinct event ids."""
    a = event_id_for_delivery(delivery_id="dlv-1", event_type="CHECKS.FAILED")
    b = event_id_for_delivery(delivery_id="dlv-2", event_type="CHECKS.FAILED")
    assert a != b
