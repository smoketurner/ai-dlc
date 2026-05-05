"""Shared Pydantic validators used across agent models."""

from __future__ import annotations

import json
from typing import Any


def none_to_empty_list(v: Any) -> Any:
    """Coerce list fields the model handed us in non-list shapes.

    Strands' ``structured_output`` (and other Bedrock structured-output
    paths) occasionally hands us list fields as:

    * ``None`` — the model didn't populate the field at all. Pydantic's
      ``default_factory`` only fires for missing fields, not explicit
      ``None``.
    * a JSON-encoded string — the model serialised the list back through
      a string-typed tool slot. We parse it.

    Anything else (real lists, well-formed input) passes through to
    Pydantic's normal validation.
    """
    if v is None:
        return []
    if isinstance(v, str):
        try:
            decoded = json.loads(v)
        except json.JSONDecodeError:
            return v
        if decoded is None:
            return []
        return decoded
    return v
