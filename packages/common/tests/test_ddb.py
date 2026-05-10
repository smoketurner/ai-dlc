"""Tests for ``common.ddb`` — the shared TransactWriteItems builder."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from common.ddb import (
    PutBuilder,
    TransactWriteItemsBuilder,
    UpdateBuilder,
    deserialize_item,
    is_conditional_check_failed,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

TABLE = "ddb-builder-tests"


@pytest.fixture
def ddb() -> Iterator[DynamoDBClient]:
    """Moto-backed DynamoDB client with a single PK/SK table."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


# ---------------------------------------------------------------------------
# UpdateBuilder — expression rendering
# ---------------------------------------------------------------------------


def test_set_renders_named_clause() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("foo", "bar")
    item = update.to_item()["Update"]
    assert item["UpdateExpression"] == "SET #a0 = :v0"
    assert item["ExpressionAttributeNames"] == {"#a0": "foo"}
    assert item["ExpressionAttributeValues"] == {":v0": {"S": "bar"}}


def test_set_reuses_alias_for_same_attribute() -> None:
    """Two clauses on the same attribute share the ``#``-alias."""
    update = (
        UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"})
        .set("foo", "first")
        .set("foo", "second")
    )
    item = update.to_item()["Update"]
    assert item["ExpressionAttributeNames"] == {"#a0": "foo"}
    # Two distinct values, one alias.
    assert item["UpdateExpression"] == "SET #a0 = :v0, #a0 = :v1"


def test_set_if_not_exists_renders_function_call() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set_if_not_exists(
        "project_slug",
        "demo",
    )
    item = update.to_item()["Update"]
    assert item["UpdateExpression"] == "SET #a0 = if_not_exists(#a0, :v0)"
    assert item["ExpressionAttributeValues"][":v0"] == {"S": "demo"}


def test_list_append_renders_with_empty_seed() -> None:
    """``list_append`` carries both the empty-seed and the new-items placeholders."""
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).list_append(
        "feedback",
        ["one", "two"],
    )
    item = update.to_item()["Update"]
    assert item["UpdateExpression"] == "SET #a0 = list_append(if_not_exists(#a0, :v0), :v1)"
    assert item["ExpressionAttributeValues"][":v0"] == {"L": []}
    assert item["ExpressionAttributeValues"][":v1"] == {
        "L": [{"S": "one"}, {"S": "two"}],
    }


def test_add_renders_add_clause() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).add(
        "total_tokens",
        100,
    )
    item = update.to_item()["Update"]
    assert item["UpdateExpression"] == "ADD #a0 :v0"
    assert item["ExpressionAttributeValues"][":v0"] == {"N": "100"}


def test_remove_renders_remove_clause() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).remove(
        "delivery_ids",
    )
    item = update.to_item()["Update"]
    assert item["UpdateExpression"] == "REMOVE #a0"
    assert item["ExpressionAttributeNames"] == {"#a0": "delivery_ids"}


def test_mixed_set_add_remove_composes_one_expression() -> None:
    update = (
        UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"})
        .set("status", "done")
        .add("counter", 1)
        .remove("temp")
    )
    item = update.to_item()["Update"]
    assert item["UpdateExpression"] == "SET #a0 = :v0 ADD #a1 :v1 REMOVE #a2"


def test_condition_eq_attaches_condition() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).condition_eq(
        "current_state",
        "received",
    )
    item = update.to_item()["Update"]
    assert item["ConditionExpression"] == "#a0 = :v0"
    assert item["ExpressionAttributeValues"][":v0"] == {"S": "received"}


def test_condition_not_exists_attaches_condition() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).condition_not_exists(
        "current_state",
    )
    item = update.to_item()["Update"]
    assert item["ConditionExpression"] == "attribute_not_exists(#a0)"


def test_no_condition_omits_condition_expression() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("foo", "bar")
    item = update.to_item()["Update"]
    assert "ConditionExpression" not in item


def test_reserved_word_attributes_aliased_transparently() -> None:
    """DDB reserved words (``status``, ``type``, ``name``) don't need special-casing."""
    update = (
        UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"})
        .set("status", "ready")
        .set("type", "event")
        .set("name", "alice")
    )
    item = update.to_item()["Update"]
    assert item["ExpressionAttributeNames"] == {
        "#a0": "status",
        "#a1": "type",
        "#a2": "name",
    }


# ---------------------------------------------------------------------------
# TypeSerializer round-trips
# ---------------------------------------------------------------------------


def test_set_serialises_string() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("a", "hello")
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {"S": "hello"}


def test_set_serialises_int() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("a", 42)
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {"N": "42"}


def test_set_serialises_float_via_decimal() -> None:
    """Bare ``TypeSerializer`` rejects float; the builder normalises first."""
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("a", 0.25)
    value = update.to_item()["Update"]["ExpressionAttributeValues"][":v0"]
    assert value == {"N": "0.25"}


def test_set_serialises_decimal() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set(
        "a",
        Decimal("3.14159"),
    )
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {
        "N": "3.14159",
    }


def test_set_serialises_bool_not_as_int() -> None:
    """``bool`` is an ``int`` subclass; the serialiser must still produce ``BOOL``."""
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("flag", True)
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {
        "BOOL": True,
    }


def test_set_serialises_none() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set("a", None)
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {
        "NULL": True,
    }


def test_set_serialises_string_set() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set(
        "ids",
        {"a", "b"},
    )
    value = cast(
        "dict[str, list[str]]",
        update.to_item()["Update"]["ExpressionAttributeValues"][":v0"],
    )
    assert set(value["SS"]) == {"a", "b"}


def test_set_serialises_nested_dict() -> None:
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).set(
        "feedback",
        {"kind": "ci", "code": 7, "active": True},
    )
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {
        "M": {
            "kind": {"S": "ci"},
            "code": {"N": "7"},
            "active": {"BOOL": True},
        },
    }


def test_add_accepts_float() -> None:
    """``add`` for monetary increments needs float→Decimal normalisation too."""
    update = UpdateBuilder(table=TABLE, key={"pk": "p", "sk": "s"}).add(
        "total_cost_usd",
        0.05,
    )
    assert update.to_item()["Update"]["ExpressionAttributeValues"][":v0"] == {
        "N": "0.05",
    }


# ---------------------------------------------------------------------------
# deserialize_item round-trip
# ---------------------------------------------------------------------------


def test_deserialize_item_round_trips_scalars() -> None:
    item = {
        "name": {"S": "alice"},
        "age": {"N": "30"},
        "active": {"BOOL": True},
    }
    assert deserialize_item(item) == {"name": "alice", "age": Decimal("30"), "active": True}


def test_deserialize_item_round_trips_nested_map() -> None:
    item = {
        "feedback": {
            "M": {
                "kind": {"S": "ci"},
                "tags": {"L": [{"S": "build"}, {"S": "test"}]},
            },
        },
    }
    assert deserialize_item(item) == {
        "feedback": {"kind": "ci", "tags": ["build", "test"]},
    }


# ---------------------------------------------------------------------------
# PutBuilder
# ---------------------------------------------------------------------------


def test_put_builder_serialises_item() -> None:
    put = PutBuilder(
        table=TABLE,
        item={"pk": "RUN#1", "sk": "EVENT#a", "tokens": 100},
    )
    rendered = put.to_item()["Put"]
    assert rendered["Item"] == {
        "pk": {"S": "RUN#1"},
        "sk": {"S": "EVENT#a"},
        "tokens": {"N": "100"},
    }
    assert "ConditionExpression" not in rendered


def test_put_builder_with_condition_not_exists() -> None:
    put = PutBuilder(
        table=TABLE,
        item={"pk": "RUN#1", "sk": "EVENT#a"},
    ).condition_not_exists("sk")
    rendered = put.to_item()["Put"]
    assert rendered["ConditionExpression"] == "attribute_not_exists(#a0)"
    assert rendered["ExpressionAttributeNames"] == {"#a0": "sk"}


# ---------------------------------------------------------------------------
# TransactWriteItemsBuilder.commit — moto-backed
# ---------------------------------------------------------------------------


def test_commit_succeeds_on_clean_transaction(ddb: DynamoDBClient) -> None:
    """Update + Put on an unconditionally-empty table commits and persists."""
    put = PutBuilder(
        table=TABLE,
        item={"pk": "RUN#1", "sk": "EVENT#a", "type": "SPEC.READY"},
    ).condition_not_exists("sk")
    update = UpdateBuilder(
        table=TABLE,
        key={"pk": "RUN#1", "sk": "STATE"},
    ).set("status", "received")
    committed = TransactWriteItemsBuilder().put(put).update(update).commit(ddb)
    assert committed is True
    state = ddb.get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["status"]["S"] == "received"


def test_commit_returns_false_on_conditional_check_failure(
    ddb: DynamoDBClient,
) -> None:
    """Re-delivery: second commit hits attribute_not_exists(sk) and rolls back."""
    put = PutBuilder(
        table=TABLE,
        item={"pk": "RUN#1", "sk": "EVENT#a"},
    ).condition_not_exists("sk")
    assert TransactWriteItemsBuilder().put(put).commit(ddb) is True
    # Same key, same condition — second commit must roll back.
    put_again = PutBuilder(
        table=TABLE,
        item={"pk": "RUN#1", "sk": "EVENT#a"},
    ).condition_not_exists("sk")
    assert TransactWriteItemsBuilder().put(put_again).commit(ddb) is False


def test_commit_propagates_non_conditional_errors(ddb: DynamoDBClient) -> None:
    """A genuine DDB error (e.g. missing table) is re-raised, not swallowed."""
    put = PutBuilder(table="missing-table", item={"pk": "x", "sk": "y"})
    with pytest.raises(ClientError) as exc_info:
        TransactWriteItemsBuilder().put(put).commit(ddb)
    assert exc_info.value.response["Error"]["Code"] != "TransactionCanceledException"


def test_commit_persists_serialised_types(ddb: DynamoDBClient) -> None:
    """End-to-end: Python types round-trip through commit + get_item."""
    update = (
        UpdateBuilder(table=TABLE, key={"pk": "RUN#1", "sk": "STATE"})
        .set("status", "SPEC.READY")
        .set("cost", 0.25)
        .set("flag", True)
        .add("count", 5)
    )
    assert TransactWriteItemsBuilder().update(update).commit(ddb) is True
    item = ddb.get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#1"}, "sk": {"S": "STATE"}},
    )["Item"]
    decoded = deserialize_item(item)
    assert decoded["status"] == "SPEC.READY"
    assert decoded["cost"] == Decimal("0.25")
    assert decoded["flag"] is True
    assert decoded["count"] == Decimal("5")


# ---------------------------------------------------------------------------
# is_conditional_check_failed detector
# ---------------------------------------------------------------------------


def test_is_conditional_check_failed_true_for_ccfe_reason() -> None:
    exc = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "x"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )
    assert is_conditional_check_failed(exc) is True


def test_is_conditional_check_failed_false_for_other_codes() -> None:
    exc = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "TransactWriteItems",
    )
    assert is_conditional_check_failed(exc) is False


def test_is_conditional_check_failed_false_when_no_reason_matches() -> None:
    """CancelException without a ConditionalCheckFailed reason is something else."""
    exc = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "x"},
            "CancellationReasons": [{"Code": "ItemCollectionSizeLimitExceeded"}],
        },
        "TransactWriteItems",
    )
    assert is_conditional_check_failed(exc) is False
