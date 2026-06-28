"""ID helpers — UUID7 for time-sortable identifiers.

UUID7 is the right choice for run/event/correlation IDs because it sorts
chronologically (DynamoDB sort keys, S3 prefixes, log timelines) while still
being globally unique. We use the ``uuid-utils`` package to avoid pulling in
the standard library's ``uuid`` 4-only generator.
"""

from __future__ import annotations

from typing import NewType

import uuid_utils

# Distinct types so accidentally passing a run id where an event id is expected
# fails at type-check time. They're all backed by ``str``.
RunId = NewType("RunId", str)
EventId = NewType("EventId", str)
CorrelationId = NewType("CorrelationId", str)


def _uuid7() -> str:
    """Return a fresh UUID7 as a hyphenated string."""
    return str(uuid_utils.uuid7())


def new_run_id() -> RunId:
    """Generate a new run identifier."""
    return RunId(_uuid7())


def new_event_id() -> EventId:
    """Generate a new event identifier."""
    return EventId(_uuid7())


def new_correlation_id() -> CorrelationId:
    """Generate a new correlation id (threads through events end-to-end)."""
    return CorrelationId(_uuid7())
