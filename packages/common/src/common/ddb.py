"""Fluent DynamoDB TransactWriteItems builder.

Centralises the ``SET`` / ``ADD`` / ``REMOVE`` clause stitching, attribute-
name aliasing, value-placeholder allocation, conditional-check detection,
and Python-to-DDB type marshalling that ``event_projector`` and
``state_router`` previously implemented twice by hand.

Callers pass Python values; the builder serialises them through
``boto3.dynamodb.types.TypeSerializer``. Floats are normalised to
``Decimal`` first because DDB's Number type is decimal and
``TypeSerializer`` rejects ``float`` outright. Attribute names are
aliased via ``#`` placeholders on every reference, so DDB reserved
words (``status``, ``type``, ``name``) work without per-call special-
casing.

Example::

    update = (
        UpdateBuilder(table="runs", key={"pk": "RUN#1", "sk": "STATE"})
        .set("status", "DESIGN.READY")
        .add("total_token_in", 4000)
        .condition_eq("current_state", "architect_running")
    )
    put = PutBuilder(
        table="runs",
        item={"pk": "RUN#1", "sk": "EVENT#abc", "type": "DESIGN.READY"},
    ).condition_not_exists("sk")
    committed = TransactWriteItemsBuilder().update(update).put(put).commit(client)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self, cast

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _normalize(value: Any) -> Any:
    """Replace ``float`` with ``Decimal`` recursively for TypeSerializer.

    ``TypeSerializer`` raises on ``float`` because DDB Number is decimal.
    Caller code routinely deals in floats (e.g. ``cost_usd``), so the
    builder normalises on the way in. ``Decimal(str(f))`` avoids the
    binary-float precision-loss path.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, set):
        return {_normalize(v) for v in value}
    return value


def _serialize(value: Any) -> dict[str, Any]:
    """Serialize a Python value into the DDB wire-format dict."""
    return cast("dict[str, Any]", _serializer.serialize(_normalize(value)))


def deserialize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a DDB item dict back into raw Python values.

    Thin wrapper over ``TypeDeserializer`` for symmetry with the
    builder's serialisation path.
    """
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


@dataclass
class UpdateBuilder:
    """Fluent builder for one ``TransactWriteItems`` Update item.

    Methods append to the internal SET / ADD / REMOVE lists and return
    ``self`` so calls chain. Attribute names are allocated ``#aN``
    aliases on first use and reused within the same builder. Value
    placeholders are ``:vN``, allocated per call (distinct values get
    distinct placeholders).
    """

    table: str
    key: dict[str, Any]
    _set_parts: list[str] = field(default_factory=list, init=False, repr=False)
    _add_parts: list[str] = field(default_factory=list, init=False, repr=False)
    _remove_parts: list[str] = field(default_factory=list, init=False, repr=False)
    _names: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _values: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _condition: str | None = field(default=None, init=False, repr=False)
    _next_name: int = field(default=0, init=False, repr=False)
    _next_value: int = field(default=0, init=False, repr=False)

    def _alias_name(self, attribute: str) -> str:
        """Allocate (or reuse) a ``#``-alias for ``attribute``."""
        for alias, name in self._names.items():
            if name == attribute:
                return alias
        alias = f"#a{self._next_name}"
        self._next_name += 1
        self._names[alias] = attribute
        return alias

    def _alias_value(self, value: Any) -> str:
        """Allocate a fresh ``:``-placeholder for ``value`` (serialised)."""
        alias = f":v{self._next_value}"
        self._next_value += 1
        self._values[alias] = _serialize(value)
        return alias

    def set(self, attribute: str, value: Any) -> Self:
        """SET ``attribute = value``."""
        name = self._alias_name(attribute)
        placeholder = self._alias_value(value)
        self._set_parts.append(f"{name} = {placeholder}")
        return self

    def set_if_not_exists(self, attribute: str, value: Any) -> Self:
        """SET ``attribute = if_not_exists(attribute, value)``."""
        name = self._alias_name(attribute)
        placeholder = self._alias_value(value)
        self._set_parts.append(f"{name} = if_not_exists({name}, {placeholder})")
        return self

    def list_append(self, attribute: str, items: list[Any]) -> Self:
        """Append ``items`` to ``attribute``, creating the list if missing.

        Renders as ``SET <attr> = list_append(if_not_exists(<attr>, :empty), :new)``
        so the first append works on rows that don't yet carry the list.
        """
        name = self._alias_name(attribute)
        empty = self._alias_value([])
        new = self._alias_value(items)
        self._set_parts.append(
            f"{name} = list_append(if_not_exists({name}, {empty}), {new})",
        )
        return self

    def add(self, attribute: str, value: Any) -> Self:
        """ADD ``attribute value`` (numeric increment or set union)."""
        name = self._alias_name(attribute)
        placeholder = self._alias_value(value)
        self._add_parts.append(f"{name} {placeholder}")
        return self

    def remove(self, attribute: str) -> Self:
        """REMOVE ``attribute``."""
        name = self._alias_name(attribute)
        self._remove_parts.append(name)
        return self

    def condition_eq(self, attribute: str, value: Any) -> Self:
        """``ConditionExpression: attribute = value``."""
        name = self._alias_name(attribute)
        placeholder = self._alias_value(value)
        self._condition = f"{name} = {placeholder}"
        return self

    def condition_not_exists(self, attribute: str) -> Self:
        """``ConditionExpression: attribute_not_exists(attribute)``."""
        name = self._alias_name(attribute)
        self._condition = f"attribute_not_exists({name})"
        return self

    def to_item(self) -> TransactWriteItemTypeDef:
        """Render the final ``TransactWriteItem`` Update dict."""
        parts: list[str] = []
        if self._set_parts:
            parts.append("SET " + ", ".join(self._set_parts))
        if self._add_parts:
            parts.append("ADD " + ", ".join(self._add_parts))
        if self._remove_parts:
            parts.append("REMOVE " + ", ".join(self._remove_parts))
        update: dict[str, Any] = {
            "TableName": self.table,
            "Key": {k: _serialize(v) for k, v in self.key.items()},
            "UpdateExpression": " ".join(parts),
            "ExpressionAttributeNames": self._names,
            "ExpressionAttributeValues": self._values,
        }
        if self._condition is not None:
            update["ConditionExpression"] = self._condition
        return cast("TransactWriteItemTypeDef", {"Update": update})


@dataclass
class PutBuilder:
    """Fluent builder for one ``TransactWriteItems`` Put item."""

    table: str
    item: dict[str, Any]
    _condition: str | None = field(default=None, init=False, repr=False)
    _names: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def condition_not_exists(self, attribute: str) -> Self:
        """``ConditionExpression: attribute_not_exists(attribute)``."""
        alias = f"#a{len(self._names)}"
        self._names[alias] = attribute
        self._condition = f"attribute_not_exists({alias})"
        return self

    def to_item(self) -> TransactWriteItemTypeDef:
        """Render the final ``TransactWriteItem`` Put dict."""
        put: dict[str, Any] = {
            "TableName": self.table,
            "Item": {k: _serialize(v) for k, v in self.item.items()},
        }
        if self._condition is not None:
            put["ConditionExpression"] = self._condition
            put["ExpressionAttributeNames"] = self._names
        return cast("TransactWriteItemTypeDef", {"Put": put})


@dataclass
class TransactWriteItemsBuilder:
    """Aggregates ``Put`` + ``Update`` items and commits them atomically."""

    items: list[TransactWriteItemTypeDef] = field(default_factory=list)

    def put(self, builder: PutBuilder) -> Self:
        """Append a ``PutBuilder``'s item to the transaction."""
        self.items.append(builder.to_item())
        return self

    def update(self, builder: UpdateBuilder) -> Self:
        """Append an ``UpdateBuilder``'s item to the transaction."""
        self.items.append(builder.to_item())
        return self

    def commit(self, client: DynamoDBClient) -> bool:
        """Commit the transaction atomically.

        Returns ``True`` on success. Returns ``False`` when any item's
        ``ConditionExpression`` evaluated false — DDB cancels the whole
        transaction; callers treat this as a re-delivery / race-loss
        no-op. Any other ``ClientError`` is re-raised.
        """
        try:
            client.transact_write_items(TransactItems=self.items)
        except ClientError as exc:
            if is_conditional_check_failed(exc):
                return False
            raise
        return True


def is_conditional_check_failed(exc: ClientError) -> bool:
    """``True`` when a ``TransactWriteItems`` was cancelled by a condition mismatch.

    DDB surfaces the cancellation as ``TransactionCanceledException`` with
    a per-item ``CancellationReasons`` list. Any ``ConditionalCheckFailed``
    reason means we lost a race or this is a re-delivery — callers treat
    both as a silent no-op.
    """
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons", []) or []
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)
