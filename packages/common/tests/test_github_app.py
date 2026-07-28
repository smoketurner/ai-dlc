"""Tests for ``common.github_app`` credential loading + caching."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from common import github_app
from common.github_app import app_credentials

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:aidlc/github-app"  # noqa: S105 - an ARN, not a credential
# Not a real PEM — ``private_key_pem`` only base64-decodes, it does not parse.
KEY_BYTES = b"fake-key-material-for-tests"
PEM_B64 = base64.b64encode(KEY_BYTES).decode()


def secret_json(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "app_id": 12345,
        "private_key_base64": PEM_B64,
        "client_id": "Iv1.abc",
        **overrides,
    }
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_GITHUB_APP_SECRET_ARN", SECRET_ARN)
    github_app.secret_cache.clear()


def install_secrets_client(monkeypatch: pytest.MonkeyPatch, *values: str) -> MagicMock:
    """Wire a Secrets Manager stub that returns ``values`` in order."""
    client = MagicMock()
    client.get_secret_value.side_effect = [{"SecretString": v} for v in values]
    monkeypatch.setattr(github_app, "secrets_client", lambda: client)
    return client


def test_reads_and_parses_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    install_secrets_client(monkeypatch, secret_json())

    creds = app_credentials()

    assert creds.app_id == 12345
    assert creds.private_key_pem() == KEY_BYTES


def test_second_call_within_ttl_hits_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    client = install_secrets_client(monkeypatch, secret_json())

    first = app_credentials()
    second = app_credentials()

    assert first is second
    client.get_secret_value.assert_called_once()


def test_malformed_secret_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad secret must not poison the cache for the full TTL.

    ``strict=True`` rejects a string ``app_id``. If the raw value were
    cached before validation, correcting the secret in Secrets Manager
    would not recover a warm process until the TTL expired.
    """
    client = install_secrets_client(
        monkeypatch,
        secret_json(app_id="12345"),
        secret_json(),
    )

    with pytest.raises(ValidationError):
        app_credentials()
    assert github_app.secret_cache == {}

    creds = app_credentials()

    assert creds.app_id == 12345
    assert client.get_secret_value.call_count == 2


def test_expired_entry_is_refetched(monkeypatch: pytest.MonkeyPatch) -> None:
    client = install_secrets_client(monkeypatch, secret_json(), secret_json(app_id=999))

    assert app_credentials().app_id == 12345
    # Expire the entry rather than patching the global clock.
    creds, _ = github_app.secret_cache[SECRET_ARN]
    github_app.secret_cache[SECRET_ARN] = (creds, 0.0)

    assert app_credentials().app_id == 999
    assert client.get_secret_value.call_count == 2


def test_binary_secret_is_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretBinary": secret_json().encode()}
    monkeypatch.setattr(github_app, "secrets_client", lambda: client)

    assert app_credentials().app_id == 12345


def test_non_string_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretBinary": 42}
    monkeypatch.setattr(github_app, "secrets_client", lambda: client)

    with pytest.raises(TypeError, match="Expected SecretString"):
        app_credentials()
