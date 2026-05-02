"""Tests for implementer.tasks — parser + checkbox flipper."""

from __future__ import annotations

import pytest

from implementer.tasks import find_task, mark_done, parse_tasks

SAMPLE = """\
# Tasks — Add /healthz

> **Spec slug:** `add-healthz`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add /healthz route
  - **Implements:** AC-R-001-a
  - **Touches:** `services/dashboard/src/dashboard/routes/health.py`
  - **Done when:** curl /healthz returns 200 {ok: true}

- [x] **T-002** — Wire route in app
  - **Implements:** AC-R-001-a, AC-R-001-b
  - **Touches:** `services/dashboard/src/dashboard/app.py`
  - **Done when:** new route registered in app

- [ ] **T-003** — Add unit test
  - **Implements:** AC-R-001-a
  - **Done when:** pytest passes
"""


def test_parse_three_tasks() -> None:
    rows = parse_tasks(SAMPLE)
    assert [r.id for r in rows] == ["T-001", "T-002", "T-003"]


def test_parse_done_state() -> None:
    rows = parse_tasks(SAMPLE)
    assert rows[0].done is False
    assert rows[1].done is True
    assert rows[2].done is False


def test_parse_implements_and_touches() -> None:
    rows = parse_tasks(SAMPLE)
    t2 = rows[1]
    assert t2.implements == ["AC-R-001-a", "AC-R-001-b"]
    assert t2.touches == ["services/dashboard/src/dashboard/app.py"]
    assert t2.done_when == "new route registered in app"


def test_find_task_hit() -> None:
    rows = parse_tasks(SAMPLE)
    found = find_task(rows, "T-002")
    assert found is not None
    assert found.title == "Wire route in app"


def test_find_task_miss() -> None:
    rows = parse_tasks(SAMPLE)
    assert find_task(rows, "T-999") is None


def test_mark_done_flips_checkbox() -> None:
    out = mark_done(SAMPLE, "T-001")
    assert "- [x] **T-001** — Add /healthz route" in out
    # Other tasks unchanged.
    assert "- [x] **T-002**" in out
    assert "- [ ] **T-003**" in out


def test_mark_done_already_done_raises() -> None:
    with pytest.raises(KeyError):
        mark_done(SAMPLE, "T-002")


def test_mark_done_missing_task_raises() -> None:
    with pytest.raises(KeyError):
        mark_done(SAMPLE, "T-999")


def test_parse_skips_random_text() -> None:
    """The parser must ignore prose, headings, blockquotes, etc."""
    text = (
        "# Heading\n\nSome words here.\n\n"
        "- [ ] **T-001** — Real task\n"
        "  - **Implements:** AC\n"
        "  - **Done when:** ok\n"
    )
    rows = parse_tasks(text)
    assert len(rows) == 1
    assert rows[0].id == "T-001"


def test_round_trip_mark_done() -> None:
    """Marking T-001 done and re-parsing should show all three the same way."""
    flipped = mark_done(SAMPLE, "T-001")
    rows = parse_tasks(flipped)
    assert rows[0].done is True
    assert rows[1].done is True
    assert rows[2].done is False
