"""Strands hooks for the Tester.

Mirrors the Reviewer's cap: ``read_spec_doc`` at most 3 times per
invocation. Mapping acceptance criteria to tests should not require
re-reading the same documents over and over.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import ToolCallCounter

READ_SPEC_DOC_CAP = 3


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"read_spec_doc": READ_SPEC_DOC_CAP}),
    ]
