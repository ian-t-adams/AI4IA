"""Process liveness and cached canonical-store readiness."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from fastapi import APIRouter, Request, Response, status

from ..sessions.repository import SessionRepository

router = APIRouter(tags=["health"])

_READINESS_TIMEOUT_SECONDS = 2.0
_READINESS_SUCCESS_TTL_SECONDS = 15.0
_READINESS_FAILURE_TTL_SECONDS = 2.0


class SessionStoreReadiness:
    """Bound and coalesce the dependency check used by ACA readiness probes."""

    def __init__(
        self,
        repo: SessionRepository,
        *,
        timeout_seconds: float = _READINESS_TIMEOUT_SECONDS,
        success_ttl_seconds: float = _READINESS_SUCCESS_TTL_SECONDS,
        failure_ttl_seconds: float = _READINESS_FAILURE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._timeout_seconds = timeout_seconds
        self._success_ttl_seconds = success_ttl_seconds
        self._failure_ttl_seconds = failure_ttl_seconds
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._expires_at = 0.0
        self._ready = False

    async def check(self) -> bool:
        now = self._monotonic()
        if now < self._expires_at:
            return self._ready
        async with self._lock:
            now = self._monotonic()
            if now < self._expires_at:
                return self._ready
            try:
                await asyncio.wait_for(
                    self._repo.check_ready(), timeout=self._timeout_seconds
                )
            except Exception:  # noqa: BLE001 - readiness fails closed
                self._ready = False
                ttl = self._failure_ttl_seconds
            else:
                self._ready = True
                ttl = self._success_ttl_seconds
            self._expires_at = self._monotonic() + ttl
            return self._ready


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    probe: SessionStoreReadiness = request.app.state.session_readiness
    if await probe.check():
        return {"status": "ok", "stage": "session_store"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "unavailable", "stage": "session_store"}
