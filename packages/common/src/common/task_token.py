"""Step Functions ``waitForTaskToken`` callback helpers.

Long-running AgentCore Runtime invocations exceed the AWS SDK HTTP client's
read timeout when called synchronously from Step Functions. The pipeline
sidesteps that by routing those calls through ``runtime_invoker``
(``lambda:invoke.waitForTaskToken``), which embeds the SF task token into
the agent payload. The agent reads the token off its input and uses these
helpers to report success or failure directly to Step Functions when the
work completes — even if that's minutes (or hours) later.

Long-running tasks must also keep the SF state alive — the per-state
``HeartbeatSeconds`` will fire ``States.Timeout`` if no
``SendTaskHeartbeat`` arrives in that window. Use :func:`heartbeat_loop`
as a context manager to spawn a background thread that ticks
periodically while real work happens on the main thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Iterator
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions.client import SFNClient

logger = structlog.get_logger()

# How often to ping ``SendTaskHeartbeat``. Must be < HeartbeatSeconds in
# the ASL (currently 600s) with comfortable margin.
HEARTBEAT_INTERVAL_SECONDS = 60.0


@cache
def sfn_client() -> SFNClient:
    """Process-cached Step Functions client."""
    return boto3.client("stepfunctions", region_name=os.environ["AWS_REGION"])


def send_task_success(*, task_token: str, output: dict[str, Any]) -> None:
    """Report a successful agent invocation back to Step Functions."""
    sfn_client().send_task_success(
        taskToken=task_token,
        output=json.dumps(output),
    )
    logger.info("send_task_success", token_prefix=task_token[:12])


def send_task_failure(*, task_token: str, exc: BaseException) -> None:
    """Report a failed agent invocation back to Step Functions.

    ``error`` and ``cause`` are the two strings SF surfaces in the failure
    event; we use the exception type name + message respectively, both
    truncated to fit SF's per-field limits.
    """
    error = type(exc).__name__[:256]
    cause = (str(exc) or repr(exc))[:32_768]
    sfn_client().send_task_failure(
        taskToken=task_token,
        error=error,
        cause=cause,
    )
    logger.warning("send_task_failure", error=error, token_prefix=task_token[:12])


@contextlib.contextmanager
def heartbeat_loop(task_token: str | None) -> Iterator[None]:
    """Tick ``SendTaskHeartbeat`` on a background thread until the body returns.

    No-op when ``task_token`` is ``None`` (synchronous-mode invocations
    without a token don't need heartbeats). Errors from the heartbeat
    call are logged and swallowed — a transient SF blip should not abort
    the agent's actual work.
    """
    if task_token is None:
        yield
        return
    stop = threading.Event()
    thread = threading.Thread(
        target=_tick_heartbeat,
        kwargs={"task_token": task_token, "stop": stop},
        daemon=True,
        name="task-token-heartbeat",
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5.0)


def _tick_heartbeat(*, task_token: str, stop: threading.Event) -> None:
    """Loop body for :func:`heartbeat_loop` — fires until ``stop`` is set."""
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            sfn_client().send_task_heartbeat(taskToken=task_token)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "?")
            # Once SF has resolved the gate (success or timeout), further
            # heartbeats become TaskTimedOut / TaskDoesNotExist. Log and
            # exit the loop — the work will resolve via SendTaskSuccess
            # or the outer except branch.
            logger.warning("heartbeat failed", code=code, token_prefix=task_token[:12])
            if code in {"TaskDoesNotExist", "TaskTimedOut"}:
                return
