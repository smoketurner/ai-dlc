"""Unit tests for dashboard.shutdown_signal."""

from __future__ import annotations

import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import cast


def test_is_shutting_down_starts_false() -> None:
    import dashboard.shutdown_signal as mod  # noqa: PLC0415

    assert mod.is_shutting_down is False


def test_sigterm_flips_flag() -> None:
    # Reload the module so the flag starts at False regardless of test order.
    mod_name = "dashboard.shutdown_signal"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    import dashboard.shutdown_signal as mod  # noqa: PLC0415

    assert mod.is_shutting_down is False

    # Simulate SIGTERM by calling the registered handler directly.
    raw = signal.getsignal(signal.SIGTERM)
    assert callable(raw), "SIGTERM handler must be callable"
    cast("Callable[[int, FrameType | None], None]", raw)(signal.SIGTERM, None)

    assert mod.is_shutting_down is True

    # Reset so other tests are not affected.
    mod.is_shutting_down = False
