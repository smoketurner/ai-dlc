"""Shared fixtures for e2e smoke tests."""

import os

import httpx  # ty: ignore[unresolved-import]  # added in T-003
import pytest


@pytest.fixture(scope="session")
def base_url() -> str:
    """Base URL of the deployed service under test."""
    url = os.environ.get("SMOKE_TEST_API_URL", "")
    if not url:
        pytest.skip("SMOKE_TEST_API_URL not set")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def auth_headers(base_url: str) -> dict[str, str]:
    """Auth headers built from SMOKE_TEST_API_KEY."""
    key = os.environ.get("SMOKE_TEST_API_KEY", "")
    if not key:
        pytest.skip("SMOKE_TEST_API_KEY not set")
    return {"X-Api-Key": key}


@pytest.fixture(scope="session")
def http_client(base_url: str, auth_headers: dict[str, str]) -> httpx.Client:
    """Synchronous httpx client pre-configured with base URL and auth headers."""
    with httpx.Client(base_url=base_url, headers=auth_headers, timeout=30.0) as client:
        yield client
