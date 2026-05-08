"""Liveness probe endpoint — no authentication required."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> PlainTextResponse:
    """ALB health-check endpoint."""
    return PlainTextResponse("ok")
