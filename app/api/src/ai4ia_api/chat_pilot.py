"""Narrow API boundary for the isolated shared-gateway chat pilot."""
from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


_SESSION_PATH = re.compile(
    r"/api/sessions/(?!initializations(?:/|\Z)|deletions(?:/|\Z))[^/]+(?:/messages)?\Z"
)


class IsolatedChatPilot:
    """Expose only chat, model catalog, and process-local session operations."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        allowed = (
            (method == "GET" and path in {"/health/live", "/health/ready", "/api/models"})
            or (path == "/api/sessions" and method in {"GET", "POST"})
            or (method == "GET" and _SESSION_PATH.fullmatch(path) is not None)
            or (path == "/api/chat" and method == "POST")
        )
        if not allowed:
            await JSONResponse(
                {"detail": "This route is unavailable in isolated chat-pilot mode.",
                 "code": "chat_pilot_route_disabled"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)
