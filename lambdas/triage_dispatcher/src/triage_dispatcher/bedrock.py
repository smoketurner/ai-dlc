"""Bedrock Converse-API helper for invoking Haiku and parsing JSON output."""

from __future__ import annotations

import json
import os
from functools import cache
from typing import Any

import boto3
from pydantic import ValidationError

from triage_dispatcher.models import TriageVerdict

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@cache
def runtime() -> Any:
    """Process-cached bedrock-runtime client."""
    return boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``.

    Triage runs on Haiku 4.5 by default — short context, low-tool, cheap to
    iterate. Override via env var if a project benefits from a stronger
    classifier.
    """
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def classify(*, system_prompt: str, user_message: str) -> TriageVerdict:
    """Invoke Bedrock Converse with JSON-mode output and parse the verdict.

    Raises ``ValueError`` if the model returns malformed JSON or a payload
    that doesn't validate against :class:`TriageVerdict`.
    """
    response = runtime().converse(
        modelId=model_id(),
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={
            "temperature": 0.0,
            "maxTokens": 2048,
        },
    )
    text = extract_text(response)
    payload = extract_json(text)
    try:
        return TriageVerdict.model_validate(payload)
    except ValidationError as exc:
        msg = f"Triage verdict failed validation: {exc}"
        raise ValueError(msg) from exc


def extract_text(response: dict[str, Any]) -> str:
    """Pull the assistant text out of a Bedrock Converse response."""
    output = response.get("output", {})
    message = output.get("message", {})
    contents = message.get("content", [])
    parts = [c.get("text", "") for c in contents if "text" in c]
    return "".join(parts).strip()


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text``.

    Models sometimes wrap JSON in ```` ``` ```` fences or prose; we tolerate
    both as long as the JSON is the only object-shaped span in the output.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        msg = f"no JSON object found in model output: {text[:200]!r}"
        raise ValueError(msg)
    blob = text[start : end + 1]
    try:
        result = json.loads(blob)
    except json.JSONDecodeError as exc:
        msg = f"model returned invalid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(result, dict):
        msg = f"model returned non-object JSON: {type(result).__name__}"
        raise TypeError(msg)
    return result
