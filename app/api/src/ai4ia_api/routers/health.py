"""Liveness/readiness probes. Liveness never touches Azure."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    state = request.app.state
    return {
        "status": "ok",
        "auth_provider": state.settings.auth_provider.value,
        "session_store": state.settings.session_store.value,
        "env": state.settings.env.value,
    }
