"""Tests for the GET /healthz liveness endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app)


def test_healthz_status_code() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_body() -> None:
    response = client.get("/healthz")
    assert response.json() == {"status": "ok"}


def test_healthz_content_type() -> None:
    response = client.get("/healthz")
    assert "application/json" in response.headers["content-type"]
