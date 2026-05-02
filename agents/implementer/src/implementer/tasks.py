"""Parse + update the tasks.md checklist.

The Implementer reads ``tasks.md`` to find the task it was invoked for, runs
the work, and then flips that task's checkbox from ``[ ]`` to ``[x]`` before
committing.

The tasks.md format the Architect emits is::

    - [ ] **T-001** — Add /healthz route
      - **Implements:** AC-R-001-a
      - **Touches:** `path/to/file.py`
      - **Done when:** curl /healthz returns 200 {ok: true}

We parse the leading checkbox line + the indented sub-bullets. Other text is
preserved verbatim.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

TASK_LINE = re.compile(r"^- \[(?P<state>[ x])\] \*\*(?P<id>T-\d{3,})\*\* — (?P<title>.+)$")
SUBBULLET = re.compile(r"^  - \*\*(?P<key>[^:*]+):\*\* (?P<value>.+)$")


class TaskRow(BaseModel):
    """One row in tasks.md."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: Annotated[str, Field(pattern=r"^T-\d{3,}$")]
    title: str
    done: bool = False
    implements: list[str] = Field(default_factory=list)
    touches: list[str] = Field(default_factory=list)
    done_when: str | None = None


def parse_tasks(markdown: str) -> list[TaskRow]:
    """Parse a ``tasks.md`` body into structured rows.

    Lines that don't match the expected shape are silently ignored — the
    document may include a heading, a blockquote, and free-form prose that
    isn't task data.
    """
    rows: list[TaskRow] = []
    current: dict[str, object] | None = None
    for line in markdown.splitlines():
        match = TASK_LINE.match(line)
        if match is not None:
            if current is not None:
                rows.append(TaskRow.model_validate(current))
            current = new_row(match)
            continue
        if current is not None:
            apply_subbullet(current, line)
    if current is not None:
        rows.append(TaskRow.model_validate(current))
    return rows


def new_row(match: re.Match[str]) -> dict[str, object]:
    """Bootstrap a parser scratch dict from a checkbox-line regex match."""
    return {
        "id": match.group("id"),
        "title": match.group("title").strip(),
        "done": match.group("state") == "x",
        "implements": [],
        "touches": [],
    }


def apply_subbullet(current: dict[str, object], line: str) -> None:
    """Merge a sub-bullet line into the current parser scratch dict."""
    sub = SUBBULLET.match(line)
    if sub is None:
        return
    key = sub.group("key").strip().lower()
    value = sub.group("value").strip()
    if key == "implements":
        current["implements"] = [v.strip() for v in value.split(",") if v.strip()]
    elif key == "touches":
        current["touches"] = [v.strip().strip("`") for v in value.split(",") if v.strip()]
    elif key == "done when":
        current["done_when"] = value


def find_task(rows: list[TaskRow], task_id: str) -> TaskRow | None:
    """Return the row whose id matches ``task_id``, or ``None``."""
    for row in rows:
        if row.id == task_id:
            return row
    return None


def mark_done(markdown: str, task_id: str) -> str:
    """Return ``markdown`` with the given task's checkbox flipped to ``[x]``.

    Raises ``KeyError`` if the task isn't found in the document.
    """
    pattern = re.compile(rf"^(- \[) \] (\*\*{re.escape(task_id)}\*\*)", re.MULTILINE)
    new_text, n = pattern.subn(r"\1x] \2", markdown, count=1)
    if n == 0:
        msg = f"task_id={task_id!r} not found or already complete"
        raise KeyError(msg)
    return new_text
