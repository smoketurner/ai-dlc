"""Unit tests for the /healthz liveness endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app)


def test_healthz_status() -> None:
    """GET /healthz returns 200."""
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_content_type() -> None:
    """GET /healthz returns text/plain; charset=utf-8."""
    response = client.get("/healthz")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


def test_healthz_body() -> None:
    """GET /healthz body is exactly 'ok'."""
    response = client.get("/healthz")
    assert response.text == "ok"
