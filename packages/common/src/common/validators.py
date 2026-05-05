"""Shared Pydantic validators used across agent models."""

from __future__ import annotations


def none_to_empty_list[T](v: list[T] | None) -> list[T]:
    """Coerce ``None`` to ``[]`` for optional list fields.

    Strands' ``structured_output`` (and other Bedrock structured-output
    paths) sometimes hand us ``None`` for list fields the model chose
    not to populate. Pydantic's ``default_factory`` only fires when the
    field is missing, not when it's explicitly ``None`` — so list fields
    that the model could omit need an explicit ``BeforeValidator`` to
    accept ``None`` as "empty list".
    """
    return v if v is not None else []
