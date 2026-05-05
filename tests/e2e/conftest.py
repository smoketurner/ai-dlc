"""Sys-path fixup so tests/e2e can import agent packages without installing them."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ARCHITECT_SRC = _REPO / "agents" / "architect" / "src"
if str(_ARCHITECT_SRC) not in sys.path:
    sys.path.insert(0, str(_ARCHITECT_SRC))
