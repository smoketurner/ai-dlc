"""SIGTERM handler that flips ``is_shutting_down`` to True.

The healthz route imports this flag to return 503 once the process receives
SIGTERM, giving the load balancer time to drain connections before the
container exits.
"""

from __future__ import annotations

import signal

is_shutting_down: bool = False


def _handle_sigterm(signum: int, frame: object) -> None:
    global is_shutting_down  # noqa: PLW0603
    is_shutting_down = True


signal.signal(signal.SIGTERM, _handle_sigterm)
