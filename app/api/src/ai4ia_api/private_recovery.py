"""A private-network candidate can prove readiness without accepting work."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


async def require_private_cosmos(endpoint: str) -> None:
    """Runtime DNS proof precedes the existing authenticated canonical-store read."""
    url = urlsplit(endpoint)
    host = url.hostname or ""
    if url.scheme != "https" or not host.endswith(".documents.azure.com") or url.username or url.password:
        raise ValueError("Private Cosmos requires its canonical HTTPS account endpoint.")
    rows = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ranges = tuple(ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if not rows or any(
        not any(ipaddress.ip_address(row[4][0]) in network for network in ranges)
        for row in rows
    ):
        raise ValueError("Cosmos did not resolve exclusively to private VNet addresses.")


class PrivateRecoveryCandidate:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http" and (
            scope["method"] != "GET"
            or scope["path"] not in {"/health/live", "/health/ready", "/api/decisions/options"}
        ):
            await JSONResponse(
                {"detail": "Private recovery candidate does not accept application work.",
                 "code": "private_recovery_candidate"},
                status_code=503, headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)
