"""Step Functions ``waitForTaskToken`` callback helpers.

Long-running AgentCore Runtime invocations exceed the AWS SDK HTTP client's
read timeout when called synchronously from Step Functions. The pipeline
sidesteps that by routing those calls through ``runtime_invoker``
(``lambda:invoke.waitForTaskToken``), which embeds the SF task token into
the agent payload. The agent reads the token off its input and uses these
helpers to report success or failure directly to Step Functions when the
work completes — even if that's minutes (or hours) later.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions.client import SFNClient

logger = structlog.get_logger()


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
