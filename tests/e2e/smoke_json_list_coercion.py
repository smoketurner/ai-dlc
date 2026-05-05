"""End-to-end smoke tests for JSON-string list coercion.

Sends two payloads to the deployed service:
  1. A field whose value is a JSON-encoded string representing a list.
  2. A field whose value is already a native list.

Both must return 200 with the field coerced to (or preserved as) a native list.
"""

import json

import httpx
import pytest

_ITEMS = ["alpha", "beta", "gamma"]


def test_json_string_list_is_coerced(http_client: httpx.Client) -> None:
    """Service coerces a JSON-encoded string list to a native list."""
    payload = {"items": json.dumps(_ITEMS)}
    response = http_client.post("/", json=payload)
    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}: {response.text}"
    )
    body = response.json()
    items = body["items"]
    assert isinstance(items, list), f"Expected list after coercion, got {type(items)!r}: {items!r}"
    assert items == _ITEMS, f"Expected {_ITEMS!r} but got {items!r}"


def test_native_list_passthrough(http_client: httpx.Client) -> None:
    """Service preserves a natively-typed list field unchanged."""
    payload = {"items": _ITEMS}
    response = http_client.post("/", json=payload)
    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}: {response.text}"
    )
    body = response.json()
    items = body["items"]
    assert isinstance(items, list), f"Expected list, got {type(items)!r}: {items!r}"
    assert items == _ITEMS, f"Expected {_ITEMS!r} but got {items!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
