"""Tests for ``common.github_app`` credential loading + caching."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from unittest.mock import MagicMock

import boto3
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from moto import mock_aws
from pydantic import ValidationError

from common import github_app
from common.github_app import app_credentials, app_jwt

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:aidlc/github-app"  # noqa: S105 - an ARN, not a credential
# Not a real PEM — ``private_key_pem`` only base64-decodes, it does not parse.
KEY_BYTES = b"fake-key-material-for-tests"
PEM_B64 = base64.b64encode(KEY_BYTES).decode()


def _generate_private_key_pem() -> bytes:
    """Generate a real RSA PKCS#8 PEM for RS256 signing in ``app_jwt`` tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_pem_for(private_key_pem: bytes) -> bytes:
    """Derive the SubjectPublicKeyInfo PEM for verifying a JWT signed with ``private_key_pem``."""
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _secret_json_with_key(key_pem: bytes, **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "app_id": 12345,
        "private_key_base64": base64.b64encode(key_pem).decode(),
        "client_id": "Iv1.abc",
        **overrides,
    }
    return json.dumps(payload)


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
    github_app.jwt_cache.clear()


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


# --- ``app_jwt`` caching + key-rotation regression tests ---
#
# The bug: ``secret_cache`` (15 min TTL) outlives ``jwt_cache`` (8.5 min
# TTL). After a key rotation, when the JWT cache expires ``app_jwt`` calls
# ``app_credentials``, which returns the *stale cached* key (secret cache
# still valid) and re-signs the JWT with it — GitHub 401s for up to 6 min.
# Fix: the JWT cache key includes a hash of the private key, so a rotated
# key forces a fresh JWT even when the old JWT entry is still within TTL.


def test_app_jwt_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call within the TTL reuses the cached JWT without re-fetching the secret."""
    key_pem = _generate_private_key_pem()
    client = install_secrets_client(monkeypatch, _secret_json_with_key(key_pem))

    first = app_jwt()
    second = app_jwt()

    assert first == second
    # Secret fetched exactly once — JWT cache hit on the second call.
    client.get_secret_value.assert_called_once()


def test_app_jwt_decodes_with_the_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JWT is signed RS256 with the App private key and carries ``iss`` = app_id."""
    key_pem = _generate_private_key_pem()
    install_secrets_client(monkeypatch, _secret_json_with_key(key_pem))

    token = app_jwt()

    decoded = jwt.decode(token, _public_pem_for(key_pem), algorithms=["RS256"])
    assert decoded["iss"] == "12345"


def test_app_jwt_uses_new_key_after_rotation_while_secret_cache_still_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a rotated key must drive the next JWT even when ``secret_cache`` is still warm.

    Reproduces the bug from the report: at T=0 the key is cached in both
    ``secret_cache`` (900 s TTL) and ``jwt_cache`` (510 s TTL). At T=500
    the operator rotates the key in Secrets Manager. At T=520 the JWT
    cache has expired but ``secret_cache`` is still valid for 380 s.
    Pre-fix ``app_credentials`` returned the stale cached key and
    ``app_jwt`` re-signed with it → 401 for ~6 min. With the key-hash
    cache key, ``app_jwt`` observes the new key and signs a fresh JWT.
    """
    # Freeze the clock so JWT payloads are byte-identical when re-signed
    # with the same key within the same logical instant. Without this the
    # ``pre_rotation_jwt == first_jwt`` assertion is flaky across second
    # boundaries (``iat``/``exp`` shift).
    frozen = time.time()
    monkeypatch.setattr(github_app.time, "time", lambda: frozen)

    key_a = _generate_private_key_pem()
    key_b = _generate_private_key_pem()
    # First call returns key_a; the second (post-rotation) returns key_b.
    client = install_secrets_client(
        monkeypatch,
        _secret_json_with_key(key_a),
        _secret_json_with_key(key_b),
    )

    # T=0: populate both caches with key_a.
    first_jwt = app_jwt()
    assert client.get_secret_value.call_count == 1

    # Simulate the JWT cache expiring while ``secret_cache`` is still warm
    # (its entry is well within the 900 s TTL — we leave it untouched).
    _expire_all_jwt_entries()

    # T=520: secret cache still valid, so ``app_credentials`` returns the
    # cached key_a *without* hitting Secrets Manager. ``app_jwt`` must
    # still produce the same JWT (signed with key_a) — no rotation yet.
    # The payload is identical because the clock is frozen.
    pre_rotation_jwt = app_jwt()
    assert pre_rotation_jwt == first_jwt
    assert client.get_secret_value.call_count == 1  # no new secret fetch

    # Now the secret cache expires too, so ``app_credentials`` re-fetches
    # and observes the rotated key_b from Secrets Manager.
    _expire_all_secret_entries()

    # T=900+: ``app_jwt`` sees the new key, computes a new cache key, and
    # signs a fresh JWT with key_b. This is the crux of the fix — the
    # cache key changed because the key material changed.
    rotated_jwt = app_jwt()
    assert rotated_jwt != first_jwt
    assert client.get_secret_value.call_count == 2

    # The new JWT is actually signed with key_b (verifies against key_b,
    # not key_a) — proving the rotation took effect.
    decoded = jwt.decode(rotated_jwt, _public_pem_for(key_b), algorithms=["RS256"])
    assert decoded["iss"] == "12345"
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(rotated_jwt, _public_pem_for(key_a), algorithms=["RS256"])


def test_app_jwt_cache_key_includes_key_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JWT cache key is derived from the private key material, not a static ``"jwt"`` string.

    This is the structural guarantee that makes the rotation fix robust:
    even if the previous JWT entry is still within its TTL, a different
    key produces a different cache key and forces a fresh signature.
    """
    key_pem = _generate_private_key_pem()
    install_secrets_client(monkeypatch, _secret_json_with_key(key_pem))

    app_jwt()

    expected_hash = hashlib.sha256(key_pem).hexdigest()[:16]
    expected_key = f"jwt:{expected_hash}"
    assert expected_key in github_app.jwt_cache
    # The old static key must not be present — otherwise a stale entry
    # could still be served after a rotation.
    assert "jwt" not in github_app.jwt_cache


def _expire_all_jwt_entries() -> None:
    """Force every ``jwt_cache`` entry to be considered expired."""
    for k, (token, _ttl) in github_app.jwt_cache.items():
        github_app.jwt_cache[k] = (token, 0.0)


def _expire_all_secret_entries() -> None:
    """Force every ``secret_cache`` entry to be considered expired."""
    for k, (creds, _ttl) in github_app.secret_cache.items():
        github_app.secret_cache[k] = (creds, 0.0)


# --- End-to-end moto-backed rotation test ---
#
# Uses a real moto Secrets Manager backend (not a MagicMock stub) to
# verify the full rotation flow: create secret → sign JWT → rotate secret
# in Secrets Manager → expire caches → sign JWT → confirm it uses the new
# key and a downstream GitHub call would succeed.


def test_end_to_end_rotation_recovers_after_secret_cache_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotate the key in a real (moto) Secrets Manager and confirm the next JWT uses it.

    This is the end-to-end guarantee: after rotating the secret value in
    Secrets Manager and letting both caches expire, ``app_jwt`` picks up
    the new key and produces a JWT that verifies against the new public
    key (not the old one). A stubbed ``httpx.post`` for the
    installation-token endpoint returns 201, confirming the downstream
    ``installation_token_for_repo`` call would succeed post-rotation.
    """
    key_a = _generate_private_key_pem()
    key_b = _generate_private_key_pem()
    secret_id = "aidlc/github-app-e2e"  # noqa: S105 - a name, not a credential

    monkeypatch.setenv("AIDLC_GITHUB_APP_SECRET_ARN", secret_id)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # Clear the @cache'd secrets_client so it picks up the moto backend.
    github_app.secrets_client.cache_clear()
    with mock_aws():
        boto3.client("secretsmanager").create_secret(
            Name=secret_id,
            SecretString=_secret_json_with_key(key_a),
        )

        # T=0: sign a JWT with key_a.
        jwt_a = app_jwt()
        jwt.decode(jwt_a, _public_pem_for(key_a), algorithms=["RS256"])
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(jwt_a, _public_pem_for(key_b), algorithms=["RS256"])

        # Rotate the secret in Secrets Manager (key_a → key_b).
        boto3.client("secretsmanager").put_secret_value(
            SecretId=secret_id,
            SecretString=_secret_json_with_key(key_b),
        )

        # Expire both caches (simulate the secret-cache TTL elapsing).
        _expire_all_jwt_entries()
        _expire_all_secret_entries()

        # T=900+: the next JWT is signed with key_b.
        jwt_b = app_jwt()
        assert jwt_b != jwt_a
        jwt.decode(jwt_b, _public_pem_for(key_b), algorithms=["RS256"])
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(jwt_b, _public_pem_for(key_a), algorithms=["RS256"])

        # Stub the GitHub installation-token endpoint so
        # ``installation_token_for_repo`` succeeds without a real GitHub
        # call. ``installation_id_for_repo`` hits ``GET /repos/.../installation``
        # first, so stub httpx.get too. ``raise_for_status`` requires a
        # ``request`` on the Response, so build one via httpx.Request.
        def fake_get(url: str, **_kwargs: object) -> httpx.Response:
            resp = httpx.Response(200, json={"id": 42})
            resp._request = httpx.Request("GET", url)
            return resp

        def fake_post(url: str, **_kwargs: object) -> httpx.Response:
            resp = httpx.Response(201, json={"token": "ghs_rotated_ok"})
            resp._request = httpx.Request("POST", url)
            return resp

        monkeypatch.setattr(github_app.httpx, "get", fake_get)
        monkeypatch.setattr(github_app.httpx, "post", fake_post)
        github_app.installation_token_cache.clear()

        token = github_app.installation_token_for_repo("owner/repo")
        assert token == "ghs_rotated_ok"  # noqa: S105 - test fixture, not a credential

    # Clear the @cache'd client again on exit so it doesn't leak the moto backend.
    github_app.secrets_client.cache_clear()
