"""Voice Live (Phase 10): a governed WebSocket relay for real-time speech-to-speech.

The browser cannot reach the upstream Azure realtime endpoint directly without
either leaking the gateway credential or bypassing the model gateway (and with it
all governance). The Next.js HTTP proxy can't proxy WebSockets either. So the
browser opens a WebSocket to the API's external ingress at ``/api/voice/live`` and
this relay:

1. refuses immediately when the feature is disabled (default OFF -> inert),
2. validates the browser ``Origin`` against a configurable allowlist (WS handshakes
   are not CORS-preflighted, so the relay must check Origin itself),
3. extracts + validates the caller's token from a WebSocket subprotocol (a
   browser-direct WS can't set an ``Authorization`` header, and bypasses the
   proxy that injects ``X-Dev-User``),
4. resolves the realtime deployment from the catalog (the browser never sees it),
5. runs the entitlement gate BEFORE opening the upstream socket,
6. opens the upstream realtime WS through the model gateway with the gateway
   credential, meters one "unknown" call for the session, then pumps text+binary
   frames in both directions until either side closes (with an optional hard clamp
   on session duration).

The event protocol itself stays client-driven (the relay is a mostly-transparent
pump): the browser sends ``session.update`` / ``input_audio_buffer.append`` and
receives ``response.audio.delta`` etc. The relay owns only the connection,
governance, and metering — never the conversation shape.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import aiohttp
import anyio
from fastapi import APIRouter, WebSocket

from ..auth.base import AuthCredentials, AuthError, AuthenticatedUser
from ..catalog import DeploymentOption, ModelCatalog
from ..config import Environment, GatewayAuthMode, Settings
from ..logging_setup import new_correlation_id
from ..usage.models import TokenUsage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice-live"])

# Catalog category whose models serve the realtime relay (gpt-realtime,
# gpt-realtime-mini). Distinct from the turn-based STT/TTS categories.
REALTIME_CATEGORIES = {"realtime"}

# WebSocket subprotocols the client offers to carry credentials (a browser WS
# can't set request headers). The first token is the marker (echoed back as the
# selected subprotocol); the second is the credential.
#  - entra: ["ai4ia-bearer", "<access_token>"]
#  - dev:   ["ai4ia-dev", "<dev_user_id>"]  (honored only when dev auth is permitted)
BEARER_SUBPROTOCOL = "ai4ia-bearer"
DEV_SUBPROTOCOL = "ai4ia-dev"

# Close codes (RFC 6455). 1008 = policy violation (denied), 1011 = internal error.
WS_NORMAL_CLOSURE = 1000
WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011

# Metering session id for live-voice traffic (mirrors voice-speech/voice-transcription).
LIVE_SESSION_ID = "voice-live"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without any IO).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthSubprotocol:
    marker: str
    credential: str


def parse_auth_subprotocols(offered: Sequence[str]) -> AuthSubprotocol | None:
    """Parse the offered subprotocols into a credential, or ``None`` if absent.

    Expects ``[marker, credential, ...]`` where ``marker`` is one of the known
    auth markers and ``credential`` is non-empty.
    """
    if len(offered) < 2:
        return None
    marker = offered[0].strip()
    credential = offered[1].strip()
    if marker not in (BEARER_SUBPROTOCOL, DEV_SUBPROTOCOL) or not credential:
        return None
    return AuthSubprotocol(marker=marker, credential=credential)


def origin_allowed(
    origin: str | None, allowed: Sequence[str], *, reflect_when_unset: bool
) -> bool:
    """Decide whether a handshake ``Origin`` may proceed.

    A configured allowlist is exact-match only. An empty allowlist reflects
    (allows any, including a missing Origin) only when ``reflect_when_unset`` is
    true (local dev); otherwise it rejects everything (fail-closed in deployed
    environments).
    """
    if allowed:
        return origin is not None and origin in allowed
    return reflect_when_unset


class RealtimeResolutionError(Exception):
    """Raised when a realtime deployment can't be resolved from the catalog."""


def resolve_realtime_deployment(
    catalog: ModelCatalog, model_id: str | None, region: str | None
) -> tuple[str, DeploymentOption]:
    """Resolve ``(model_id, deployment)`` for the realtime relay.

    Defaults to the first ``realtime`` catalog model when none is requested, and
    rejects non-realtime / unknown / unavailable models.
    """
    if not model_id:
        first = next((m for m in catalog.models if m.category in REALTIME_CATEGORIES), None)
        if first is None:
            raise RealtimeResolutionError("No realtime models are available.")
        model_id = first.id
    entry = catalog.get(model_id)
    if entry is None:
        raise RealtimeResolutionError(f"Unknown model: {model_id}")
    if entry.category not in REALTIME_CATEGORIES:
        raise RealtimeResolutionError(f"Model '{model_id}' is not a realtime model.")
    deployment = catalog.resolve_deployment(model_id, region=region)
    if deployment is None:
        raise RealtimeResolutionError(f"Unknown or unavailable model: {model_id}")
    return model_id, deployment


def _to_ws_scheme(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    return url


def build_upstream_url(base_url: str, api_version: str, deployment_name: str) -> str:
    """Construct the Azure OpenAI realtime (preview) WebSocket URL.

    ``{ws_base}/realtime?api-version=<v>&deployment=<dep>`` where the base already
    carries the ``/openai`` suffix (the model gateway URL). http(s) is converted
    to ws(s).
    """
    ws_base = _to_ws_scheme(base_url.rstrip("/"))
    return (
        f"{ws_base}/realtime"
        f"?api-version={quote(api_version, safe='')}"
        f"&deployment={quote(deployment_name, safe='')}"
    )


def build_upstream_headers(
    auth_mode: GatewayAuthMode, api_key: str | None, correlation_id: str | None
) -> dict[str, str]:
    """Mirror :meth:`ModelGatewayClient._auth_headers` for the upstream WS: APIM
    subscription key for ``api_key`` mode, bearer for ``bearer`` mode. The browser
    never sees these — they are applied server-side on the pre-handshake request."""
    headers: dict[str, str] = {}
    if correlation_id:
        headers["x-correlation-id"] = correlation_id
    if auth_mode == GatewayAuthMode.api_key and api_key:
        headers["Ocp-Apim-Subscription-Key"] = api_key
    elif auth_mode == GatewayAuthMode.bearer and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _session_usage() -> TokenUsage:
    """Realtime reports no token usage, so a session meters as one *unknown* call
    (counts toward rate limits, adds no tokens), like the REST voice endpoints."""
    return TokenUsage(known=False, complete=False, calls=1)


async def authenticate_subprotocol(
    provider, settings: Settings, auth: AuthSubprotocol
) -> AuthenticatedUser:
    """Validate the subprotocol credential via the wired auth provider.

    The dev marker is honored ONLY when dev auth is permitted, and carries the
    (spoofable) user id the same way the HTTP dev provider reads ``X-Dev-User``.
    The bearer marker is validated as a real token (entra JWT).
    """
    if auth.marker == DEV_SUBPROTOCOL:
        if not settings.dev_auth_permitted:
            raise AuthError("Dev subprotocol is not permitted in this environment.")
        creds = AuthCredentials(token=None, headers={"X-Dev-User": auth.credential})
    else:
        creds = AuthCredentials(token=auth.credential, headers={})
    return await provider.authenticate(creds)


# --------------------------------------------------------------------------- #
# Upstream connector abstraction (so the relay pump is IO-agnostic and tests can
# inject a fake socket without any network).
# --------------------------------------------------------------------------- #


@dataclass
class UpstreamMessage:
    kind: str  # "text" | "binary" | "close"
    text: str | None = None
    data: bytes | None = None


class UpstreamConnection(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def receive(self) -> UpstreamMessage: ...
    async def close(self) -> None: ...


class RealtimeConnector(Protocol):
    def connect(
        self, *, url: str, headers: dict[str, str], timeout: float
    ) -> AbstractAsyncContextManager[UpstreamConnection]: ...


class _AiohttpUpstream:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def send_text(self, data: str) -> None:
        await self._ws.send_str(data)

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def receive(self) -> UpstreamMessage:
        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            return UpstreamMessage("text", text=msg.data)
        if msg.type == aiohttp.WSMsgType.BINARY:
            return UpstreamMessage("binary", data=msg.data)
        # CLOSE/CLOSING/CLOSED/ERROR all terminate the relay.
        return UpstreamMessage("close")

    async def close(self) -> None:
        await self._ws.close()


class AiohttpRealtimeConnector:
    """Production connector: opens the upstream realtime WS with aiohttp.

    A fresh ``ClientSession`` per connection is fine — realtime sessions are
    long-lived, so the per-session overhead is negligible, and it guarantees clean
    teardown. ``sock_read`` is left unbounded (an idle live session must not be
    dropped); only the connect/handshake is time-bounded.
    """

    @asynccontextmanager
    async def connect(
        self, *, url: str, headers: dict[str, str], timeout: float
    ) -> AsyncIterator[UpstreamConnection]:
        client_timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.ws_connect(url, headers=headers) as ws:
                yield _AiohttpUpstream(ws)


# --------------------------------------------------------------------------- #
# Bidirectional pump.
#
# Starlette runs on anyio, so the relay uses an anyio task group rather than raw
# asyncio tasks: when one direction ends it cancels the group's scope, and — just
# as importantly — the framework's own teardown cancellation (e.g. the client
# going away) stays anyio-native and is recognized by the enclosing cancel scope
# instead of leaking out and cancelling the whole request task.
# --------------------------------------------------------------------------- #


async def _pump_client_to_upstream(
    client_ws: WebSocket, upstream: UpstreamConnection, cancel_scope: anyio.CancelScope
) -> None:
    try:
        while True:
            message = await client_ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is not None:
                await upstream.send_text(text)
                continue
            data = message.get("bytes")
            if data is not None:
                await upstream.send_bytes(data)
    finally:
        # The client side is done -> stop the upstream pump too.
        cancel_scope.cancel()


async def _pump_upstream_to_client(
    upstream: UpstreamConnection, client_ws: WebSocket, cancel_scope: anyio.CancelScope
) -> None:
    try:
        while True:
            msg = await upstream.receive()
            if msg.kind == "close":
                return
            if msg.kind == "text" and msg.text is not None:
                await client_ws.send_text(msg.text)
            elif msg.kind == "binary" and msg.data is not None:
                await client_ws.send_bytes(msg.data)
    finally:
        # The upstream side is done -> stop the client pump too.
        cancel_scope.cancel()


async def relay(
    client_ws: WebSocket, upstream: UpstreamConnection, *, max_seconds: float
) -> None:
    """Pump frames both ways until either side closes (or the optional clamp)."""

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_pump_client_to_upstream, client_ws, upstream, tg.cancel_scope)
            tg.start_soon(_pump_upstream_to_client, upstream, client_ws, tg.cancel_scope)

    if max_seconds and max_seconds > 0:
        with anyio.move_on_after(max_seconds) as scope:
            await run()
        if scope.cancelled_caught:
            logger.info("voice-live session hit max duration clamp (%ss)", max_seconds)
    else:
        await run()


# --------------------------------------------------------------------------- #
# Route.
# --------------------------------------------------------------------------- #


async def _deny(client_ws: WebSocket, code: int) -> None:
    try:
        await client_ws.close(code=code)
    except RuntimeError:
        # Already closed/disconnected; nothing to do.
        pass


@router.websocket("/api/voice/live")
async def voice_live(websocket: WebSocket) -> None:
    state = websocket.app.state
    settings: Settings = state.settings

    # 1. Feature flag: inert by default. Refuse before doing anything else.
    if not settings.realtime_enabled:
        await _deny(websocket, WS_POLICY_VIOLATION)
        return

    # 2. Origin allowlist (WS handshakes are not CORS-preflighted).
    if not origin_allowed(
        websocket.headers.get("origin"),
        settings.realtime_allowed_origin_list,
        reflect_when_unset=settings.env == Environment.local,
    ):
        await _deny(websocket, WS_POLICY_VIOLATION)
        return

    # 3. Auth: extract + validate the token from the subprotocol.
    auth = parse_auth_subprotocols(websocket.scope.get("subprotocols") or [])
    if auth is None:
        await _deny(websocket, WS_POLICY_VIOLATION)
        return
    try:
        user = await authenticate_subprotocol(state.auth_provider, settings, auth)
    except AuthError:
        await _deny(websocket, WS_POLICY_VIOLATION)
        return

    # 4. Resolve the realtime deployment (browser never sees it).
    try:
        model_id, deployment = resolve_realtime_deployment(
            state.catalog,
            websocket.query_params.get("model"),
            websocket.query_params.get("region"),
        )
    except RealtimeResolutionError:
        await _deny(websocket, WS_POLICY_VIOLATION)
        return

    # 5. Entitlement gate BEFORE opening the upstream socket.
    decision = await state.entitlements.check(user.internal_user_id)
    if not decision.allowed:
        await _deny(websocket, WS_POLICY_VIOLATION)
        return

    # Handshake complete: echo the auth marker as the selected subprotocol.
    await websocket.accept(subprotocol=auth.marker)

    correlation_id = new_correlation_id()
    url = build_upstream_url(
        settings.realtime_effective_base_url,
        settings.realtime_api_version,
        deployment.deploymentName,
    )
    headers = build_upstream_headers(
        settings.model_gateway_auth_mode, settings.model_gateway_api_key, correlation_id
    )
    connector: RealtimeConnector = state.realtime_connector

    try:
        async with connector.connect(
            url=url, headers=headers, timeout=settings.realtime_timeout_seconds
        ) as upstream:
            # Meter one unknown call per opened session (best-effort, never raises).
            await state.usage.record_completion(
                user_id=user.internal_user_id,
                session_id=LIVE_SESSION_ID,
                model_id=model_id,
                deployment=deployment,
                usage=_session_usage(),
                status="complete",
                correlation_id=correlation_id,
            )
            await relay(
                websocket, upstream, max_seconds=settings.realtime_max_session_seconds
            )
    except Exception:  # noqa: BLE001 - any upstream/relay failure -> clean client close
        logger.warning(
            "voice-live relay error (model=%s, correlation_id=%s)",
            model_id,
            correlation_id,
            exc_info=True,
        )
        await _deny(websocket, WS_INTERNAL_ERROR)
        return

    await _deny(websocket, WS_NORMAL_CLOSURE)
