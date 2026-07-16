"""Voice Live: a governed WebSocket relay for real-time speech-to-speech.

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
   credential, pumps text+binary frames in both directions until either side closes
   (with an optional hard clamp), then meters one classified "unknown" call.

The event protocol itself stays client-driven (the relay is a mostly-transparent
pump): the browser sends ``session.update`` / ``input_audio_buffer.append`` and
receives ``response.audio.delta`` etc. The relay owns only the connection,
governance, metering, and — when the session is bound to an agent (``?agent=``) —
the server-authoritative persona instructions + tool allowlist injected into the
client's ``session.update``. It never drives the turn-by-turn conversation shape.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol
from urllib.parse import quote

import aiohttp
import anyio
from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..agents.agent_catalog import AgentSpec
from ..agents.tool_exec import ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry
from ..auth.base import AuthCredentials, AuthError, AuthenticatedUser
from ..catalog import DeploymentOption, ModelCatalog
from ..config import Environment, GatewayAuthMode, Settings
from ..logging_setup import new_correlation_id, set_correlation_id
from ..voice_provider_catalog import (
    AZURE_OPENAI_PROVIDER_ID,
    SPEECH_VOICE_LIVE_PROVIDER_ID,
    AzureOpenAIVoiceProvider,
    SpeechVoiceProvider,
    VoiceProvider,
    VoiceProviderCatalog,
    VoiceProviderManagedModel,
    load_voice_provider_catalog,
)
from ..usage.models import TokenUsage, UsageStatus, UsageTarget

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
# Subprotocol values must be RFC 7230 tokens (no "@", spaces, etc.), so the dev
# identity is base64url-encoded with a "b64u." prefix by the browser whenever it
# is not already a bare token (e.g. an email like "dev@ai4ia.local"). Bearer
# (entra) tokens are JWTs whose base64url segments + "." separators are already
# valid tokens, so they ride unencoded. decode_dev_credential() reverses this so
# the live session resolves to the SAME internal user id as the HTTP path.
BEARER_SUBPROTOCOL = "ai4ia-bearer"
DEV_SUBPROTOCOL = "ai4ia-dev"
_DEV_CREDENTIAL_B64URL_PREFIX = "b64u."


def decode_dev_credential(credential: str) -> str:
    """Reverse the browser's token-safe encoding of the dev identity.

    The browser base64url-encodes the dev user id (prefixing it with ``b64u.``)
    only when it is not already a valid WebSocket subprotocol token. A bare id
    (no prefix) is returned unchanged, keeping older clients + plain ids working.
    A malformed encoded value falls back to the raw credential rather than
    raising, so a bad encoding degrades to a denied/unknown user rather than a
    500 during the handshake.
    """
    if not credential.startswith(_DEV_CREDENTIAL_B64URL_PREFIX):
        return credential
    encoded = credential[len(_DEV_CREDENTIAL_B64URL_PREFIX) :]
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return credential

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


@dataclass(frozen=True)
class LiveVoiceProviderResolution:
    provider: VoiceProvider
    model_id: str
    deployment: DeploymentOption | None
    managed_model: VoiceProviderManagedModel | None
    usage_target: UsageTarget
    base_url: str
    api_version: str
    target_param: str
    target_name: str
    auth_mode: GatewayAuthMode
    api_key: str
    rewrite_client_frame: Callable[[str], str | None]


class LiveVoiceProviderError(Exception):
    """Raised when the browser requests an unknown or disabled voice provider."""


def _resolve_live_voice_provider(
    state,
    settings: Settings,
    provider_id: str | None,
    *,
    model: str | None,
    region: str | None,
) -> LiveVoiceProviderResolution:
    catalog: VoiceProviderCatalog = getattr(
        state, "voice_provider_catalog", load_voice_provider_catalog()
    )
    requested = (provider_id or settings.voice_default_provider_id).strip().lower()
    allowlist = set(settings.voice_provider_allowlist_list)
    if requested not in allowlist:
        raise LiveVoiceProviderError(f"Provider '{requested}' is not enabled.")
    provider = catalog.get(requested)
    if provider is None:
        raise LiveVoiceProviderError(f"Unknown voice provider: {requested}")

    if requested == AZURE_OPENAI_PROVIDER_ID:
        if not isinstance(provider, AzureOpenAIVoiceProvider):
            raise LiveVoiceProviderError("Azure OpenAI voice catalog entry is invalid.")
        model_id, deployment = resolve_realtime_deployment(
            state.catalog,
            model,
            region,
        )
        return LiveVoiceProviderResolution(
            provider=provider,
            model_id=model_id,
            deployment=deployment,
            managed_model=None,
            usage_target=UsageTarget.from_deployment(deployment),
            base_url=settings.realtime_effective_base_url,
            api_version=settings.realtime_api_version,
            target_param="deployment",
            target_name=deployment.deploymentName,
            auth_mode=settings.model_gateway_auth_mode,
            api_key=settings.realtime_gateway_api_key or "",
            rewrite_client_frame=lambda frame: frame,
        )

    if requested != SPEECH_VOICE_LIVE_PROVIDER_ID:
        raise LiveVoiceProviderError(f"Unknown voice provider: {requested}")
    if not settings.speech_voice_live_enabled:
        raise LiveVoiceProviderError("Speech Voice Live is disabled.")
    if not settings.speech_voice_live_base_url or not settings.speech_voice_live_gateway_api_key:
        raise LiveVoiceProviderError("Speech Voice Live is not fully configured.")
    if not isinstance(provider, SpeechVoiceProvider):
        raise LiveVoiceProviderError("Speech Voice Live catalog entry is invalid.")

    selected_model_id = provider.defaultManagedModelId if model is None else model
    managed = provider.get_managed_model(selected_model_id)
    if managed is None:
        raise LiveVoiceProviderError("Speech Voice Live model is not available.")
    if region is not None and region != managed.initialRegion:
        raise LiveVoiceProviderError("Speech Voice Live model is not available in that region.")

    def rewrite_client_frame(frame: str) -> str | None:
        return normalize_speech_client_frame(frame, provider, managed)

    return LiveVoiceProviderResolution(
        provider=provider,
        model_id=managed.id,
        deployment=None,
        managed_model=managed,
        usage_target=UsageTarget.managed_service(
            provider=SPEECH_VOICE_LIVE_PROVIDER_ID,
            target="managed_voice_live",
            region=managed.initialRegion,
        ),
        base_url=settings.speech_voice_live_base_url,
        api_version=managed.apiVersion,
        target_param="model",
        target_name=managed.id,
        auth_mode=GatewayAuthMode.api_key,
        api_key=settings.speech_voice_live_gateway_api_key,
        rewrite_client_frame=rewrite_client_frame,
    )


# Truthy spellings accepted for the per-session ``?tools=`` opt-in, matching the
# browser's parseEnabledFlag / the server feature-flag env parsing.
_TOOLS_TRUTHY = frozenset({"1", "true", "yes", "on"})


def parse_tools_opt_in(value: str | None) -> bool:
    """Whether the browser opted into governed tools for this session (``?tools=``).

    Default OFF: an absent/empty/unrecognized value means tools are NOT requested,
    so even with the server ``realtime_tools_enabled`` flag on the relay stays a
    transparent pump until the user explicitly enables tools in the live panel.
    """
    return bool(value) and value.strip().lower() in _TOOLS_TRUTHY


def _to_ws_scheme(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    return url


def build_upstream_url(
    base_url: str,
    api_version: str,
    target_name: str,
    *,
    target_param: str = "deployment",
) -> str:
    """Construct a realtime WebSocket URL.

    ``{ws_base}/realtime?api-version=<v>&<target_param>=<name>`` where the base
    already carries the provider-specific APIM prefix (``/openai`` or
    ``/speech/voice-live``). http(s) is converted to ws(s).
    """
    ws_base = _to_ws_scheme(base_url.rstrip("/"))
    return (
        f"{ws_base}/realtime"
        f"?api-version={quote(api_version, safe='')}"
        f"&{target_param}={quote(target_name, safe='')}"
    )


def _speech_locale(provider: SpeechVoiceProvider, session: dict[str, Any]) -> str:
    options = list(provider.capabilities.locale.options)
    default = provider.sessionDefaults.locale
    raw_voice = session.get("voice")
    candidates = [
        raw_voice.get("locale") if isinstance(raw_voice, dict) else None,
        session.get("locale"),
        session.get("language"),
    ]
    for raw in candidates:
        if isinstance(raw, str) and raw.strip() in options:
            return raw.strip()
    return default


def _speech_session_voice(
    provider: SpeechVoiceProvider, session: dict[str, Any], locale: str
) -> dict[str, Any]:
    default_name = provider.capabilities.voices.default
    allowed = set(provider.capabilities.voices.options)
    raw = session.get("voice")
    selected = default_name
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate in allowed:
            selected = candidate
    elif isinstance(raw, dict):
        candidate_name = raw.get("name")
        candidate_type = raw.get("type")
        if isinstance(candidate_name, str):
            candidate_name = candidate_name.strip()
        if (
            isinstance(candidate_name, str)
            and candidate_name in allowed
            and (candidate_type in (None, provider.capabilities.voices.kind))
        ):
            selected = candidate_name
    return {
        "type": provider.capabilities.voices.kind,
        "name": selected,
        "locale": locale,
    }


def _speech_turn_detection(
    provider: SpeechVoiceProvider, session: dict[str, Any]
) -> dict[str, Any]:
    default = provider.capabilities.turnDetection.default
    allowed = set(provider.capabilities.turnDetection.options)
    raw = session.get("turn_detection")
    selected = default
    raw_settings = raw if isinstance(raw, dict) else {}
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate in allowed:
            selected = candidate
    elif isinstance(raw, dict):
        for key in ("type", "mode", "kind"):
            candidate = raw.get(key)
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if candidate in allowed:
                    selected = candidate
                    break
    turn_detection: dict[str, Any] = {
        "type": selected,
        "create_response": True,
        "interrupt_response": _speech_bool(
            raw_settings.get("interrupt_response", session.get("interrupt_response")),
            provider.sessionDefaults.interruptResponse,
        ),
        "auto_truncate": _speech_bool(
            raw_settings.get("auto_truncate", session.get("auto_truncate")),
            provider.sessionDefaults.autoTruncate,
        ),
    }
    threshold = raw_settings.get("threshold")
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        turn_detection["threshold"] = max(0.0, min(float(threshold), 1.0))
    silence_ms = raw_settings.get("silence_duration_ms")
    if isinstance(silence_ms, (int, float)) and not isinstance(silence_ms, bool):
        turn_detection["silence_duration_ms"] = max(0, min(int(silence_ms), 60_000))
    return turn_detection


def _speech_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _speech_simple_option(
    raw: object,
    *,
    options: Sequence[str],
    default: str,
) -> str:
    candidate: object = raw
    if isinstance(raw, dict):
        candidate = raw.get("type")
    if isinstance(candidate, str) and candidate in options:
        return candidate
    return default


def normalize_speech_client_frame(
    frame: str,
    provider: SpeechVoiceProvider,
    managed_model: VoiceProviderManagedModel | None = None,
) -> str | None:
    """Parse and normalize every browser text frame for Voice Live Speech.

    Every frame is decoded before forwarding to avoid parser differentials and
    escaped event-type bypasses. Session configuration is rebuilt from catalog
    allowlists, while response.create is reduced to a configuration-free trigger so
    per-response voices, tools, endpoint ids, and other overrides cannot reach Azure.
    Malformed/non-event JSON is rejected by returning ``None``.
    """
    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        logger.info("voice-live rejected a non-JSON Speech client frame")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        logger.info("voice-live rejected a Speech client frame without an event type")
        return None
    event_type = payload["type"]
    if event_type == RESPONSE_CREATE_TYPE:
        return json.dumps({"type": RESPONSE_CREATE_TYPE})
    if event_type != SESSION_UPDATE_TYPE:
        return json.dumps(payload)
    requested = payload.get("session")
    session = dict(requested) if isinstance(requested, dict) else {}
    selected_model = managed_model or provider.get_managed_model(
        provider.defaultManagedModelId
    )
    if selected_model is None:
        logger.error("voice-live Speech catalog has no default managed model")
        return None
    locale = _speech_locale(provider, session)
    normalized: dict[str, Any] = {
        "voice": _speech_session_voice(provider, session, locale),
        "input_audio_transcription": {
            "model": selected_model.inputTranscription.model,
            "language": locale,
        },
        "turn_detection": _speech_turn_detection(provider, session),
        "input_audio_format": selected_model.audioFormat,
        "output_audio_format": selected_model.audioFormat,
        "input_audio_sampling_rate": selected_model.sampleRateHz,
        "modalities": ["text", "audio"],
    }
    instructions = session.get("instructions")
    if isinstance(instructions, str):
        normalized["instructions"] = instructions
    temperature = session.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        normalized["temperature"] = max(0.0, min(float(temperature), 2.0))
    normalized["input_audio_noise_reduction"] = {
        "type": _speech_simple_option(
            session.get("input_audio_noise_reduction"),
            options=provider.capabilities.noiseSuppression.options,
            default=provider.sessionDefaults.noiseSuppression,
        )
    }
    normalized["input_audio_echo_cancellation"] = {
        "type": _speech_simple_option(
            session.get("input_audio_echo_cancellation"),
            options=provider.capabilities.echoCancellation.options,
            default=provider.sessionDefaults.echoCancellation,
        )
    }
    return json.dumps({"type": SESSION_UPDATE_TYPE, "session": normalized})


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
        dev_user = decode_dev_credential(auth.credential)
        creds = AuthCredentials(token=None, headers={"X-Dev-User": dev_user})
    else:
        creds = AuthCredentials(token=auth.credential, headers={})
    return await provider.authenticate(creds)


# --------------------------------------------------------------------------- #
# Upstream connector abstraction (so the relay pump is IO-agnostic and tests can
# inject a fake socket without any network).
# --------------------------------------------------------------------------- #


UpstreamMessageKind = Literal["text", "binary", "close", "error"]
_SAFE_TEXT_MAX_CHARS = 512
_SAFE_EVENT_NAME_MAX_CHARS = 128
_SAFE_TEXT_SCAN_CHARS = 4096
_REDACTED = "[REDACTED]"
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (
        ["']?
        (?:authorization|bearer|api(?:[-_]|\s)?key|access[-_]?token|id[-_]?token|
           ocp[-_]?apim[-_]?subscription[-_]?key|subscription(?:[-_]|\s)?key|
           credential|password|secret|sig|token|key)
        ["']?
        \s*[:=]\s*
        ["']?
    )
    ([^"'\s&,;]+)
    """
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bAuthorization\s*[:=]\s*)(?:Bearer\s+)?[^\s,;]+(?:\s+[^\s,;]+)?"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)
_SAFE_EVENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def sanitize_realtime_metadata(value: object, *, max_chars: int = _SAFE_TEXT_MAX_CHARS) -> str:
    """Return bounded, control-free metadata with common credential forms redacted."""

    if not isinstance(value, str):
        return ""
    clean = "".join(
        char for char in value[:_SAFE_TEXT_SCAN_CHARS] if not unicodedata.category(char).startswith("C")
    )
    clean = _AUTHORIZATION_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", clean)
    clean = _BEARER_RE.sub(f"Bearer {_REDACTED}", clean)
    clean = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", clean)
    clean = _JWT_RE.sub(_REDACTED, clean)
    return clean[: max(0, max_chars)]


def _safe_exception_parts(exc: object) -> tuple[str | None, str | None]:
    if isinstance(exc, BaseException):
        exception_class = _safe_exception_class(type(exc).__name__)
        try:
            message = sanitize_realtime_metadata(str(exc))
        except Exception:  # noqa: BLE001 - hostile exception __str__
            message = ""
        return exception_class, message or None
    if isinstance(exc, str):
        message = sanitize_realtime_metadata(exc)
        return "UpstreamWebSocketError", message or None
    return None, None


def _safe_source_event(value: object) -> str | None:
    candidate = sanitize_realtime_metadata(value, max_chars=_SAFE_EVENT_NAME_MAX_CHARS)
    return candidate if _SAFE_EVENT_NAME_RE.fullmatch(candidate) else None


def _safe_exception_class(value: object) -> str | None:
    candidate = sanitize_realtime_metadata(value, max_chars=_SAFE_EVENT_NAME_MAX_CHARS)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", candidate):
        return None
    return candidate


def _safe_close_code(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65_535:
        return value
    return None


@dataclass(frozen=True, slots=True)
class UpstreamMessage:
    kind: UpstreamMessageKind
    text: str | None = None
    data: bytes | None = None
    close_code: int | None = None
    close_reason: str | None = None
    exception_class: str | None = None
    exception_message: str | None = None
    source_event: str | None = None

    @property
    def code(self) -> int | None:
        return self.close_code

    @property
    def reason(self) -> str | None:
        return self.close_reason

    @property
    def source_ws_event(self) -> str | None:
        return self.source_event


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
        source_event = _safe_source_event(getattr(msg.type, "name", None))
        if msg.type == aiohttp.WSMsgType.TEXT:
            text = msg.data if isinstance(msg.data, str) else ""
            return UpstreamMessage("text", text=text, source_event=source_event)
        if msg.type == aiohttp.WSMsgType.BINARY:
            data = msg.data if isinstance(msg.data, bytes) else bytes(msg.data)
            return UpstreamMessage("binary", data=data, source_event=source_event)
        if msg.type == aiohttp.WSMsgType.ERROR:
            error = msg.data if isinstance(msg.data, BaseException) else self._ws.exception()
            exception_class, exception_message = _safe_exception_parts(error or msg.data)
            return UpstreamMessage(
                "error",
                close_code=_safe_close_code(self._ws.close_code),
                exception_class=exception_class,
                exception_message=exception_message,
                source_event=source_event,
            )
        # Preserve close details for CLOSE/CLOSING/CLOSED instead of collapsing the
        # event into an untyped terminator.
        message_close_code = _safe_close_code(msg.data)
        close_code = (
            message_close_code
            if message_close_code is not None
            else _safe_close_code(self._ws.close_code)
        )
        close_reason = sanitize_realtime_metadata(msg.extra) or None
        return UpstreamMessage(
            "close",
            close_code=close_code,
            close_reason=close_reason,
            source_event=source_event,
        )

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
# Governed tool calling + agent persona inside a live session.
#
# When realtime tools are enabled the relay stops being a pure pump for exactly
# two narrow frame kinds and owns governed function calling, reusing the SAME
# tool registry + executor as chat (authorize -> validate -> run). It:
#   * rewrites the client's ``session.update`` to advertise the authorized tools
#     (flat realtime schema) + ``tool_choice: "auto"`` — scoped to the bound
#     agent's allowlist when one is selected — so the browser can never advertise
#     a tool the gateway didn't authorize, and
#   * on a ``response.function_call_arguments.done`` event, authorizes + executes
#     the call in-process and returns the result to the model via a
#     ``conversation.item.create`` (function_call_output) + ``response.create``.
# When the session is bound to an agent (``?agent=``) the same session.update
# rewrite also sets the server-authoritative persona ``instructions`` (so a voice
# turn carries the same persona as a chat @mention, and the browser can't spoof a
# different one). Every other frame (audio, transcripts, all other events) is
# forwarded verbatim, and with no tools and no agent persona the bridge is an
# inert pass-through so the relay's byte-for-byte transparent-pump behavior is preserved.
# --------------------------------------------------------------------------- #

SESSION_UPDATE_TYPE = "session.update"
RESPONSE_CREATE_TYPE = "response.create"
FUNCTION_CALL_DONE_TYPE = "response.function_call_arguments.done"
RESPONSE_CREATE_FRAME = '{"type":"response.create"}'
# Cheap pre-filters so the hot path only full-parses the two frame kinds the
# bridge owns; audio frames (``input_audio_buffer.append`` / ``response.audio.delta``)
# never contain these markers and are forwarded without a JSON parse.
_SESSION_UPDATE_HINT = '"session.update"'
_FUNCTION_CALL_HINT = '"response.function_call_arguments.done"'


@dataclass(frozen=True)
class RealtimeFunctionCall:
    call_id: str
    name: str
    arguments: str  # raw JSON string the model emitted


def flatten_realtime_tools(nested: Sequence[dict]) -> list[dict]:
    """Convert chat-completions ``{"type":"function","function":{...}}`` tool specs
    to the flat realtime shape ``{"type":"function","name",...}``.

    The realtime API declares tools flat (``name``/``description``/``parameters`` at
    the top level), unlike the nested chat-completions schema the executor emits.
    Entries without a usable function body are skipped.
    """
    out: list[dict] = []
    for entry in nested:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        flat: dict[str, Any] = {"type": "function", "name": fn["name"]}
        if fn.get("description"):
            flat["description"] = fn["description"]
        if fn.get("parameters") is not None:
            flat["parameters"] = fn["parameters"]
        out.append(flat)
    return out


def inject_session_tools(
    frame: str,
    tools: Sequence[dict],
    tool_choice: str,
    *,
    instructions: str | None = None,
) -> str:
    """Merge relay-owned fields into a client ``session.update`` frame.

    Returns the frame unchanged when it isn't a parseable session.update (the relay
    stays transparent for everything it doesn't own). The client's own session
    fields are preserved EXCEPT the ones the relay owns:

    * ``tools`` / ``tool_choice`` — so the browser can never advertise a tool the
      gateway didn't authorize, and
    * ``instructions`` — when an agent persona is bound, the relay sets the system
      instructions server-authoritatively so the browser can't spoof a different
      persona. When ``instructions`` is ``None`` the client's own value is left
      untouched (generic-assistant behavior).

    A no-op (frame returned verbatim) when there is nothing to inject — neither
    tools nor instructions.
    """
    if (not tools and instructions is None) or _SESSION_UPDATE_HINT not in frame:
        return frame
    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        return frame
    if not isinstance(payload, dict) or payload.get("type") != SESSION_UPDATE_TYPE:
        return frame
    session = payload.get("session")
    if not isinstance(session, dict):
        session = {}
    if tools:
        session["tools"] = list(tools)
        session["tool_choice"] = tool_choice
    if instructions is not None:
        session["instructions"] = instructions
    payload["session"] = session
    return json.dumps(payload)


def parse_function_call_done(frame: str) -> RealtimeFunctionCall | None:
    """Extract ``(call_id, name, arguments)`` from a function-call-done event.

    Returns ``None`` for any other frame (forwarded verbatim) or a malformed event.
    ``response.function_call_arguments.done`` carries the COMPLETE arguments, so the
    relay never has to accumulate ``.delta`` fragments.
    """
    if _FUNCTION_CALL_HINT not in frame:
        return None
    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != FUNCTION_CALL_DONE_TYPE:
        return None
    call_id = payload.get("call_id")
    name = payload.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        return None
    arguments = payload.get("arguments")
    if not isinstance(arguments, str):
        arguments = "{}"
    return RealtimeFunctionCall(call_id=call_id, name=name, arguments=arguments)


def build_function_call_output(call_id: str, output: str) -> str:
    """The ``conversation.item.create`` frame returning a tool result to the model."""
    return json.dumps(
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        }
    )


def _tool_output(result: Any) -> str:
    """Encode a tool result as the string the function_call_output ``output`` wants."""
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return json.dumps({"result": str(result)})


def _tool_error(message: str) -> str:
    return json.dumps({"error": message})


@dataclass
class ToolBridge:
    """Governed tool calling + persona policy for the live relay.

    Holds the same registry/executor as chat plus the flat realtime tool schemas
    advertised to the model and, optionally, the server-authoritative ``instructions``
    for a bound agent persona. With no tools and no instructions it is a pure
    pass-through, so the relay keeps its byte-for-byte transparent-pump
    behavior. Tools drive in-process governed execution; ``instructions`` only
    rewrites the session.update (no execution), so a persona-only bridge has
    ``enabled is False`` yet still injects the persona.
    """

    registry: ToolRegistry
    executor: ToolExecutor
    ctx: ToolContext
    tools: list[dict]
    tool_choice: str = "auto"
    instructions: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.tools)

    def rewrite_client_frame(self, frame: str) -> str:
        if not self.tools and self.instructions is None:
            return frame
        return inject_session_tools(
            frame, self.tools, self.tool_choice, instructions=self.instructions
        )

    async def handle_upstream_frame(self, frame: str) -> list[str]:
        """Upstream frames to send back for a function call, or ``[]`` to forward only."""
        if not self.tools:
            return []
        call = parse_function_call_done(frame)
        if call is None:
            return []
        output = await self._run(call)
        return [build_function_call_output(call.call_id, output), RESPONSE_CREATE_FRAME]

    async def _run(self, call: RealtimeFunctionCall) -> str:
        # Authorize through the SAME governance as chat. Built-ins are ``safe`` with
        # no scopes, but a denied/unknown tool must still fail closed to a structured
        # error the model can speak (never an unguarded execution).
        decision = self.registry.authorize(
            call.name,
            granted_scopes=self.ctx.granted_scopes,
            target_hosts=self.ctx.target_hosts,
            approved=call.name in self.ctx.approvals,
        )
        if not decision.allowed:
            reason = decision.reason.value if decision.reason else "denied"
            logger.info("voice-live tool '%s' denied (%s)", call.name, reason)
            return _tool_error(f"tool '{call.name}' is not permitted")
        try:
            args = json.loads(call.arguments) if call.arguments.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (ValueError, TypeError) as exc:
            return _tool_error(f"invalid arguments: {exc}")
        try:
            result = await self.executor.execute(call.name, args, self.ctx)
        except Exception as exc:  # noqa: BLE001 - any tool failure -> structured error
            exception_class, _ = _safe_exception_parts(exc)
            logger.info(
                "voice-live tool '%s' failed (%s)",
                call.name,
                exception_class or "Exception",
            )
            return _tool_error(str(exc))
        return _tool_output(result)


def build_tool_bridge(
    state,
    settings: Settings,
    correlation_id: str,
    *,
    tool_names: Sequence[str] | None = None,
    instructions: str | None = None,
    tools_requested: bool = True,
) -> ToolBridge:
    """Construct the relay's tool bridge from app state.

    Returns an inert bridge (empty tools + no instructions -> pass-through) when
    realtime tools are disabled OR no governed builtin is authorized, so the relay
    stays a pure pump unless tool calling is explicitly turned on alongside the
    realtime feature.

    ``tool_names`` scopes the advertised tools to a specific allowlist (an agent's
    tools); when ``None`` every registered builtin is offered (generic-assistant
    behavior). ``instructions`` binds a server-authoritative agent persona that the
    relay injects into the session.update regardless of the tools gate.

    ``tools_requested`` is the per-session client opt-in (the browser's "Allow tools
    in voice" toggle, carried as ``?tools=1``). Tools are advertised only when BOTH
    the server ``realtime_tools_enabled`` flag AND this opt-in are set, so the
    default is OFF even when an operator has enabled the feature. The persona
    ``instructions`` are independent of this gate (a persona-only session never
    advertises tools but still binds its instructions).
    """
    registry: ToolRegistry = state.tool_registry
    executor: ToolExecutor = state.tool_executor
    ctx = ToolContext(correlation_id=correlation_id)
    names = executor.names() if tool_names is None else list(tool_names)
    tools: list[dict] = []
    if settings.realtime_tools_enabled and tools_requested:
        # schema_for already drops any tool not authorized for this empty context
        # (and any name not in ``names``), so the model only ever sees tools it can
        # actually run AND that the bound agent is allowed to use.
        nested = executor.schema_for(names, registry=registry, ctx=ctx)
        tools = flatten_realtime_tools(nested)
    return ToolBridge(
        registry=registry,
        executor=executor,
        ctx=ctx,
        tools=tools,
        instructions=instructions,
    )


async def resolve_live_agent(state, user, agent_name: str) -> AgentSpec | None:
    """Resolve the live session's selected agent for this user, or ``None``.

    Composes the caller's user-defined agents on top of the curated catalog (same
    resolution chat uses), then looks the mention up case-insensitively. Returns
    ``None`` for an unknown/disabled agent or a store outage, so live voice fails
    OPEN to the generic assistant rather than breaking the session.
    """
    try:
        composed = await state.agent_service.catalog_for(user.internal_user_id, state.agents)
    except Exception:  # noqa: BLE001 - agent resolution must never break a live session
        logger.warning("voice-live agent resolution failed; using generic", exc_info=True)
        return None
    spec = composed.get(agent_name)
    if spec is None or not spec.enabled:
        return None
    return spec


async def build_session_bridge(
    state,
    settings: Settings,
    correlation_id: str,
    *,
    user,
    agent_name: str | None,
    tools_requested: bool = True,
) -> ToolBridge:
    """Build the relay bridge for a live session, agent-aware when ``agent_name`` is set.

    When the browser names an agent and it resolves for this user, the live session
    speaks as that agent: its ``systemPrompt`` becomes the server-authoritative
    session instructions and the advertised tools are scoped to the agent's own
    allowlist (so a voice turn has the SAME persona + tools as a chat @mention).
    Otherwise the session falls back to the generic assistant with every authorized
    builtin — the original transparent-pump behavior.

    ``tools_requested`` is the per-session client opt-in; it gates tool advertisement
    (combined with the server flag) without affecting the bound agent's persona.
    """
    if agent_name:
        spec = await resolve_live_agent(state, user, agent_name)
        if spec is not None:
            return build_tool_bridge(
                state,
                settings,
                correlation_id,
                tool_names=spec.tools,
                instructions=spec.systemPrompt,
                tools_requested=tools_requested,
            )
    return build_tool_bridge(
        state, settings, correlation_id, tools_requested=tools_requested
    )


# --------------------------------------------------------------------------- #
# Bidirectional pump.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProtocolErrorMetadata:
    error_type: str | None = None
    code: str | None = None
    param: str | None = None
    event_id: str | None = None
    message: str | None = None

    @property
    def type(self) -> str | None:
        return self.error_type

    def as_log_dict(self) -> dict[str, str | None]:
        return {
            "type": self.error_type,
            "code": self.code,
            "param": self.param,
            "event_id": self.event_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class RelayFrameStats:
    text_frames: int = 0
    binary_frames: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    event_types: tuple[str, ...] = ()

    def as_log_dict(self) -> dict[str, object]:
        return {
            "textFrames": self.text_frames,
            "binaryFrames": self.binary_frames,
            "firstMonotonic": self.first_monotonic,
            "lastMonotonic": self.last_monotonic,
            "eventTypes": list(self.event_types),
        }


@dataclass(frozen=True, slots=True)
class RelayStats:
    client_to_upstream: RelayFrameStats = field(default_factory=RelayFrameStats)
    upstream_to_client: RelayFrameStats = field(default_factory=RelayFrameStats)

    def as_log_dict(self) -> dict[str, object]:
        return {
            "clientToUpstream": self.client_to_upstream.as_log_dict(),
            "upstreamToClient": self.upstream_to_client.as_log_dict(),
        }


@dataclass(frozen=True, slots=True)
class RelayMetadata:
    protocol_error: ProtocolErrorMetadata | None = None
    close_code: int | None = None
    close_reason: str | None = None
    exception_class: str | None = None
    exception_message: str | None = None
    source_event: str | None = None

    def as_log_dict(self) -> dict[str, object]:
        return {
            "protocolError": (
                self.protocol_error.as_log_dict() if self.protocol_error is not None else None
            ),
            "closeCode": self.close_code,
            "closeReason": self.close_reason,
            "exceptionClass": self.exception_class,
            "exceptionMessage": self.exception_message,
            "sourceEvent": self.source_event,
        }


@dataclass(frozen=True, slots=True)
class RelayOutcome:
    status: UsageStatus
    metadata: RelayMetadata = field(default_factory=RelayMetadata)
    stats: RelayStats = field(default_factory=RelayStats)

    @property
    def close_code(self) -> int | None:
        return self.metadata.close_code

    @property
    def close_reason(self) -> str | None:
        return self.metadata.close_reason

    @property
    def protocol_error(self) -> ProtocolErrorMetadata | None:
        return self.metadata.protocol_error

    @property
    def exception_class(self) -> str | None:
        return self.metadata.exception_class

    @property
    def exception_message(self) -> str | None:
        return self.metadata.exception_message

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        status: UsageStatus = "error",
        source_event: str,
    ) -> "RelayOutcome":
        exception_class, exception_message = _safe_exception_parts(exc)
        return cls(
            status=status,
            metadata=RelayMetadata(
                exception_class=exception_class,
                exception_message=exception_message,
                source_event=_safe_source_event(source_event),
            ),
        )


@dataclass(slots=True)
class _MutableFrameStats:
    text_frames: int = 0
    binary_frames: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    event_types: list[str] = field(default_factory=list)

    def observe(self, *, text: bool, event_type: str | None = None) -> None:
        now = time.monotonic()
        if self.first_monotonic is None:
            self.first_monotonic = now
        self.last_monotonic = now
        if text:
            self.text_frames += 1
            if event_type is not None and len(self.event_types) < 32:
                self.event_types.append(event_type)
        else:
            self.binary_frames += 1

    def freeze(self) -> RelayFrameStats:
        return RelayFrameStats(
            text_frames=self.text_frames,
            binary_frames=self.binary_frames,
            first_monotonic=self.first_monotonic,
            last_monotonic=self.last_monotonic,
            event_types=tuple(self.event_types),
        )


@dataclass(frozen=True, slots=True)
class _RelayTermination:
    status: UsageStatus
    close_code: int | None = None
    close_reason: str | None = None
    exception_class: str | None = None
    exception_message: str | None = None
    source_event: str | None = None


@dataclass(slots=True)
class _RelayState:
    stopped: anyio.Event
    client_stats: _MutableFrameStats = field(default_factory=_MutableFrameStats)
    upstream_stats: _MutableFrameStats = field(default_factory=_MutableFrameStats)
    protocol_error: ProtocolErrorMetadata | None = None
    termination: _RelayTermination | None = None

    def stop(self, termination: _RelayTermination) -> None:
        if self.termination is None:
            self.termination = termination
            self.stopped.set()

    def outcome(self) -> RelayOutcome:
        termination = self.termination or _RelayTermination(
            status="cancelled", source_event="local_stop"
        )
        status: UsageStatus = "error" if self.protocol_error is not None else termination.status
        return RelayOutcome(
            status=status,
            metadata=RelayMetadata(
                protocol_error=self.protocol_error,
                close_code=_safe_close_code(termination.close_code),
                close_reason=sanitize_realtime_metadata(termination.close_reason) or None,
                exception_class=(
                    _safe_exception_class(termination.exception_class)
                ),
                exception_message=(
                    sanitize_realtime_metadata(termination.exception_message) or None
                ),
                source_event=_safe_source_event(termination.source_event),
            ),
            stats=RelayStats(
                client_to_upstream=self.client_stats.freeze(),
                upstream_to_client=self.upstream_stats.freeze(),
            ),
        )


def _validated_event_type(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > _SAFE_EVENT_NAME_MAX_CHARS:
        return None
    return value if _SAFE_EVENT_NAME_RE.fullmatch(value) else None


def _safe_protocol_field(value: object) -> str | None:
    sanitized = sanitize_realtime_metadata(value, max_chars=_SAFE_EVENT_NAME_MAX_CHARS)
    return sanitized or None


def inspect_realtime_text_frame(
    frame: str, *, include_protocol_error: bool
) -> tuple[str | None, ProtocolErrorMetadata | None]:
    """Extract only a validated event name and bounded protocol-error metadata."""

    try:
        payload = json.loads(frame)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    event_type = _validated_event_type(payload.get("type"))
    if not include_protocol_error or event_type != "error":
        return event_type, None
    error = payload.get("error")
    error_fields = error if isinstance(error, dict) else {}
    protocol_event_id = error_fields.get("event_id")
    if not isinstance(protocol_event_id, str):
        protocol_event_id = payload.get("event_id")
    return event_type, ProtocolErrorMetadata(
        error_type=_safe_protocol_field(error_fields.get("type")),
        code=_safe_protocol_field(error_fields.get("code")),
        param=_safe_protocol_field(error_fields.get("param")),
        event_id=_safe_protocol_field(protocol_event_id),
        message=(
            sanitize_realtime_metadata(error_fields.get("message")) or None
        ),
    )


def _termination_from_exception(
    exc: BaseException, *, status: UsageStatus, source_event: str
) -> _RelayTermination:
    exception_class, exception_message = _safe_exception_parts(exc)
    return _RelayTermination(
        status=status,
        exception_class=exception_class,
        exception_message=exception_message,
        source_event=_safe_source_event(source_event),
    )


def _client_termination_from_exception(exc: BaseException) -> _RelayTermination:
    if isinstance(exc, WebSocketDisconnect):
        return _RelayTermination(
            status="cancelled",
            close_code=_safe_close_code(exc.code),
            close_reason=sanitize_realtime_metadata(exc.reason) or None,
            source_event="websocket.disconnect",
        )
    exception_class, exception_message = _safe_exception_parts(exc)
    return _RelayTermination(
        status="cancelled",
        exception_class=exception_class,
        exception_message=exception_message,
        source_event="client.disconnect",
    )


async def _send_upstream(
    upstream: UpstreamConnection,
    lock: anyio.Lock,
    *,
    text: str | None = None,
    data: bytes | None = None,
) -> None:
    """Serialize every write to the upstream socket.

    Both pumps may write upstream — the client pump forwards client frames, and the
    upstream pump injects tool results — and aiohttp's ws send is not safe under
    concurrency. The lock is uncontended (so free) when tools are disabled, since
    only the client pump writes then.
    """
    async with lock:
        if text is not None:
            await upstream.send_text(text)
        elif data is not None:
            await upstream.send_bytes(data)


async def _pump_client_to_upstream(
    client_ws: WebSocket,
    upstream: UpstreamConnection,
    lock: anyio.Lock,
    rewrite_client_frame: Callable[[str], str | None],
    state: _RelayState,
) -> None:
    try:
        while True:
            try:
                message = await client_ws.receive()
            except RuntimeError as exc:
                state.stop(_client_termination_from_exception(exc))
                return
            if message["type"] == "websocket.disconnect":
                state.stop(
                    _RelayTermination(
                        status="cancelled",
                        close_code=_safe_close_code(message.get("code")),
                        close_reason=sanitize_realtime_metadata(message.get("reason")) or None,
                        source_event="websocket.disconnect",
                    )
                )
                return
            text = message.get("text")
            if text is not None:
                event_type, _ = inspect_realtime_text_frame(
                    text, include_protocol_error=False
                )
                state.client_stats.observe(text=True, event_type=event_type)
                rewritten = rewrite_client_frame(text)
                if rewritten is not None:
                    await _send_upstream(upstream, lock, text=rewritten)
                continue
            data = message.get("bytes")
            if data is not None:
                state.client_stats.observe(text=False)
                await _send_upstream(upstream, lock, data=data)
                continue
            state.stop(_RelayTermination(status="cancelled", source_event="client_stop"))
            return
    except WebSocketDisconnect as exc:
        state.stop(_client_termination_from_exception(exc))
    except Exception as exc:  # noqa: BLE001 - converted to bounded relay metadata
        state.stop(
            _termination_from_exception(
                exc, status="error", source_event="client_to_upstream"
            )
        )


async def _pump_upstream_to_client(
    upstream: UpstreamConnection,
    client_ws: WebSocket,
    lock: anyio.Lock,
    bridge: ToolBridge,
    state: _RelayState,
) -> None:
    try:
        while True:
            msg = await upstream.receive()
            if msg.kind == "close":
                state.stop(
                    _RelayTermination(
                        status=(
                            "complete"
                            if msg.close_code in (None, WS_NORMAL_CLOSURE)
                            else "error"
                        ),
                        close_code=msg.close_code,
                        close_reason=msg.close_reason,
                        source_event=msg.source_event or "upstream.close",
                    )
                )
                return
            if msg.kind == "error":
                state.stop(
                    _RelayTermination(
                        status="error",
                        close_code=msg.close_code,
                        close_reason=msg.close_reason,
                        exception_class=msg.exception_class,
                        exception_message=msg.exception_message,
                        source_event=msg.source_event or "upstream.error",
                    )
                )
                return
            if msg.kind == "text" and msg.text is not None:
                event_type, protocol_error = inspect_realtime_text_frame(
                    msg.text, include_protocol_error=True
                )
                state.upstream_stats.observe(text=True, event_type=event_type)
                if protocol_error is not None and state.protocol_error is None:
                    state.protocol_error = protocol_error
                try:
                    await client_ws.send_text(msg.text)
                except (WebSocketDisconnect, RuntimeError) as exc:
                    state.stop(_client_termination_from_exception(exc))
                    return
                # Governed tool calling: a function-call event is executed in-process
                # and its result returned upstream. No-op (and no JSON parse) for
                # every other frame, and entirely skipped when tools are disabled.
                if bridge.enabled:
                    for frame in await bridge.handle_upstream_frame(msg.text):
                        await _send_upstream(upstream, lock, text=frame)
            elif msg.kind == "binary" and msg.data is not None:
                state.upstream_stats.observe(text=False)
                try:
                    await client_ws.send_bytes(msg.data)
                except (WebSocketDisconnect, RuntimeError) as exc:
                    state.stop(_client_termination_from_exception(exc))
                    return
            else:
                state.stop(_RelayTermination(status="error", source_event="upstream.invalid"))
                return
    except WebSocketDisconnect as exc:
        state.stop(_client_termination_from_exception(exc))
    except Exception as exc:  # noqa: BLE001 - converted to bounded relay metadata
        state.stop(
            _termination_from_exception(
                exc, status="error", source_event="upstream_to_client"
            )
        )


async def relay(
    client_ws: WebSocket,
    upstream: UpstreamConnection,
    *,
    max_seconds: float,
    bridge: ToolBridge,
    rewrite_client_frame: Callable[[str], str | None] | None = None,
) -> RelayOutcome:
    """Pump frames both ways and return a content-free, typed terminal outcome."""

    send_lock = anyio.Lock()
    rewrite = rewrite_client_frame or bridge.rewrite_client_frame
    state = _RelayState(stopped=anyio.Event())

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                _pump_client_to_upstream,
                client_ws,
                upstream,
                send_lock,
                rewrite,
                state,
            )
            tg.start_soon(
                _pump_upstream_to_client,
                upstream,
                client_ws,
                send_lock,
                bridge,
                state,
            )
            await state.stopped.wait()
            tg.cancel_scope.cancel()

    cancelled_exc_class = anyio.get_cancelled_exc_class()
    try:
        if max_seconds and max_seconds > 0:
            with anyio.move_on_after(max_seconds) as scope:
                await run()
            if scope.cancelled_caught:
                state.stop(
                    _RelayTermination(
                        status="cancelled", source_event="max_duration_timeout"
                    )
                )
        else:
            await run()
    except cancelled_exc_class as exc:
        state.stop(
            _RelayTermination(status="cancelled", source_event="framework.cancelled")
        )
        try:
            setattr(exc, "_ai4ia_relay_outcome", state.outcome())
        except Exception:  # pragma: no cover - cancellation type may prohibit attributes
            pass
        raise
    return state.outcome()


# --------------------------------------------------------------------------- #
# Route.
# --------------------------------------------------------------------------- #


async def _deny(client_ws: WebSocket, code: int) -> None:
    try:
        await client_ws.close(code=code)
    except RuntimeError:
        # Already closed/disconnected; nothing to do.
        pass


def _outcome_with_exception(
    outcome: RelayOutcome | None,
    exc: BaseException,
    *,
    status: UsageStatus,
    source_event: str,
) -> RelayOutcome:
    exception_class, exception_message = _safe_exception_parts(exc)
    if outcome is None:
        return RelayOutcome.from_exception(
            exc, status=status, source_event=source_event
        )
    metadata = outcome.metadata
    resolved_status: UsageStatus = (
        "error" if metadata.protocol_error is not None else status
    )
    return RelayOutcome(
        status=resolved_status,
        metadata=RelayMetadata(
            protocol_error=metadata.protocol_error,
            close_code=metadata.close_code,
            close_reason=metadata.close_reason,
            exception_class=exception_class,
            exception_message=exception_message,
            source_event=_safe_source_event(source_event),
        ),
        stats=outcome.stats,
    )


def _emit_relay_completion(
    *,
    correlation_id: str,
    resolution: LiveVoiceProviderResolution,
    outcome: RelayOutcome,
    usage_error: tuple[str | None, str | None] | None,
) -> None:
    target = resolution.usage_target
    payload: dict[str, object] = {
        "event": "voice_live_completion",
        "correlationId": correlation_id,
        "provider": resolution.provider.id,
        "usageTarget": {
            "provider": target.provider,
            "deployment": target.deployment,
            "target": target.target,
            "region": target.region,
            "dataZone": target.dataZone,
        },
        "model": resolution.model_id,
        "outcome": outcome.status,
        "metadata": outcome.metadata.as_log_dict(),
        "stats": outcome.stats.as_log_dict(),
    }
    if usage_error is not None:
        payload["usageError"] = {
            "exceptionClass": usage_error[0],
            "exceptionMessage": usage_error[1],
        }
    logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


async def _finalize_relay(
    *,
    websocket: WebSocket,
    state,
    user: AuthenticatedUser,
    correlation_id: str,
    resolution: LiveVoiceProviderResolution,
    outcome: RelayOutcome,
) -> None:
    usage_error: tuple[str | None, str | None] | None = None
    cancelled_exc_class = anyio.get_cancelled_exc_class()
    try:
        await state.usage.record_completion(
            user_id=user.internal_user_id,
            session_id=LIVE_SESSION_ID,
            model_id=resolution.model_id,
            target=resolution.usage_target,
            usage=_session_usage(),
            status=outcome.status,
            correlation_id=correlation_id,
        )
    except cancelled_exc_class:
        raise
    except Exception as exc:  # noqa: BLE001 - metering is best effort by convention
        usage_error = _safe_exception_parts(exc)

    _emit_relay_completion(
        correlation_id=correlation_id,
        resolution=resolution,
        outcome=outcome,
        usage_error=usage_error,
    )
    close_code = WS_INTERNAL_ERROR if outcome.status == "error" else WS_NORMAL_CLOSURE
    await _deny(websocket, close_code)


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

    # 4. Resolve the provider + upstream target (browser never sees it).
    try:
        provider_resolution = _resolve_live_voice_provider(
            state,
            settings,
            websocket.query_params.get("provider"),
            model=websocket.query_params.get("model"),
            region=websocket.query_params.get("region"),
        )
    except (LiveVoiceProviderError, RealtimeResolutionError):
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
    set_correlation_id(correlation_id)
    url = build_upstream_url(
        provider_resolution.base_url,
        provider_resolution.api_version,
        provider_resolution.target_name,
        target_param=provider_resolution.target_param,
    )
    headers = build_upstream_headers(
        provider_resolution.auth_mode,
        provider_resolution.api_key,
        correlation_id,
    )
    connector: RealtimeConnector = state.realtime_connector
    # Agent-aware live voice: when the browser names an agent (?agent=), bind that
    # agent's persona + tool allowlist into the session (server-authoritative). The
    # ?tools= opt-in gates tool advertisement per session (default OFF).
    bridge = await build_session_bridge(
        state,
        settings,
        correlation_id,
        user=user,
        agent_name=websocket.query_params.get("agent"),
        tools_requested=parse_tools_opt_in(websocket.query_params.get("tools")),
    )

    def rewrite_client_frame(frame: str) -> str | None:
        provider_frame = provider_resolution.rewrite_client_frame(frame)
        if provider_frame is None:
            return None
        return bridge.rewrite_client_frame(provider_frame)

    outcome: RelayOutcome | None = None
    cancelled_exc_class = anyio.get_cancelled_exc_class()
    try:
        async with connector.connect(
            url=url, headers=headers, timeout=settings.realtime_timeout_seconds
        ) as upstream:
            outcome = await relay(
                websocket,
                upstream,
                max_seconds=settings.realtime_max_session_seconds,
                bridge=bridge,
                rewrite_client_frame=rewrite_client_frame,
            )
    except cancelled_exc_class as exc:
        partial_outcome = getattr(exc, "_ai4ia_relay_outcome", None)
        if isinstance(partial_outcome, RelayOutcome):
            outcome = partial_outcome
        metadata = outcome.metadata if outcome is not None else RelayMetadata()
        outcome = RelayOutcome(
            status=(
                "error"
                if metadata.protocol_error is not None
                else "cancelled"
            ),
            metadata=RelayMetadata(
                protocol_error=metadata.protocol_error,
                close_code=metadata.close_code,
                close_reason=metadata.close_reason,
                exception_class=metadata.exception_class,
                exception_message=metadata.exception_message,
                source_event="framework.cancelled",
            ),
            stats=outcome.stats if outcome is not None else RelayStats(),
        )
        with anyio.CancelScope(shield=True):
            await _finalize_relay(
                websocket=websocket,
                state=state,
                user=user,
                correlation_id=correlation_id,
                resolution=provider_resolution,
                outcome=outcome,
            )
        raise
    except Exception as exc:  # noqa: BLE001 - converted to bounded relay metadata
        outcome = _outcome_with_exception(
            outcome,
            exc,
            status="error",
            source_event="upstream.connect_or_relay",
        )

    if outcome is None:
        outcome = RelayOutcome(
            status="cancelled",
            metadata=RelayMetadata(source_event="local_stop"),
        )
    await _finalize_relay(
        websocket=websocket,
        state=state,
        user=user,
        correlation_id=correlation_id,
        resolution=provider_resolution,
        outcome=outcome,
    )
