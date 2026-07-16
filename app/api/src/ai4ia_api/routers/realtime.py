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
   credential, meters one "unknown" call for the session, then pumps text+binary
   frames in both directions until either side closes (with an optional hard clamp
   on session duration).

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
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import quote

import aiohttp
import anyio
from fastapi import APIRouter, WebSocket

from ..agents.agent_catalog import AgentSpec
from ..agents.tool_exec import ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry
from ..auth.base import AuthCredentials, AuthError, AuthenticatedUser
from ..catalog import DeploymentOption, ModelCatalog
from ..config import Environment, GatewayAuthMode, Settings
from ..logging_setup import new_correlation_id
from ..voice_provider_catalog import (
    AZURE_OPENAI_PROVIDER_ID,
    SPEECH_VOICE_LIVE_PROVIDER_ID,
    VoiceProvider,
    VoiceProviderCatalog,
    load_voice_provider_catalog,
)
from ..usage.models import TokenUsage, UsageTarget

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
    usage_target: UsageTarget
    base_url: str
    api_version: str
    target_param: str
    target_name: str
    auth_mode: GatewayAuthMode
    api_key: str
    rewrite_client_frame: Callable[[str], str]


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
        model_id, deployment = resolve_realtime_deployment(
            state.catalog,
            model,
            region,
        )
        return LiveVoiceProviderResolution(
            provider=provider,
            model_id=model_id,
            deployment=deployment,
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

    managed = provider.managedModel
    if managed is None:
        raise LiveVoiceProviderError("Speech Voice Live catalog entry is incomplete.")
    if model and model.strip().lower() != managed.modelId.lower():
        raise LiveVoiceProviderError("Speech Voice Live model is fixed.")
    if region and region.strip().lower() != managed.initialRegion.lower():
        raise LiveVoiceProviderError("Speech Voice Live region is fixed.")

    def rewrite_client_frame(frame: str) -> str:
        return normalize_speech_session_update(frame, provider)

    return LiveVoiceProviderResolution(
        provider=provider,
        model_id=managed.modelId,
        deployment=None,
        usage_target=UsageTarget.managed_service(
            provider=SPEECH_VOICE_LIVE_PROVIDER_ID,
            target="managed_voice_live",
            region=managed.initialRegion,
        ),
        base_url=settings.speech_voice_live_base_url,
        api_version=managed.apiVersion,
        target_param="model",
        target_name=managed.modelId,
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


def _speech_locale(provider: VoiceProvider, session: dict[str, Any]) -> str:
    options = list(provider.capabilities.locale.options) if provider.capabilities.locale else []
    default = provider.sessionDefaults.locale or (options[0] if options else "en-US")
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
    provider: VoiceProvider, session: dict[str, Any], locale: str
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


def _speech_input_transcription(
    provider: VoiceProvider, session: dict[str, Any], locale: str
) -> dict[str, Any]:
    default = provider.capabilities.inputTranscription.default
    allowed = set(provider.capabilities.inputTranscription.options)
    raw = session.get("input_audio_transcription")
    selected = default
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate in allowed:
            selected = candidate
    elif isinstance(raw, dict):
        for key in ("model", "provider", "type"):
            candidate = raw.get(key)
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if candidate in allowed:
                    selected = candidate
                    break
    return {"model": selected, "language": locale}


def _speech_turn_detection(provider: VoiceProvider, session: dict[str, Any]) -> dict[str, Any]:
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


def normalize_speech_session_update(frame: str, provider: VoiceProvider) -> str:
    """Normalize a browser ``session.update`` for Voice Live Speech.

    The browser still sends the existing Azure OpenAI-shaped payload. For Speech we
    keep the shared instruction/tool bridge but overwrite the model-specific voice
    settings with catalog-backed safe values.
    """
    if _SESSION_UPDATE_HINT not in frame:
        return frame
    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        return frame
    if not isinstance(payload, dict) or payload.get("type") != SESSION_UPDATE_TYPE:
        return frame
    requested = payload.get("session")
    session = dict(requested) if isinstance(requested, dict) else {}
    locale = _speech_locale(provider, session)
    managed = provider.managedModel
    normalized: dict[str, Any] = {
        "voice": _speech_session_voice(provider, session, locale),
        "input_audio_transcription": _speech_input_transcription(provider, session, locale),
        "turn_detection": _speech_turn_detection(provider, session),
        "input_audio_format": managed.audioFormat if managed else "pcm16",
        "output_audio_format": managed.audioFormat if managed else "pcm16",
        "input_audio_sampling_rate": managed.sampleRateHz if managed else 24_000,
        "modalities": ["text", "audio"],
    }
    instructions = session.get("instructions")
    if isinstance(instructions, str):
        normalized["instructions"] = instructions
    temperature = session.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        normalized["temperature"] = max(0.0, min(float(temperature), 2.0))
    if provider.sessionDefaults.noiseSuppression is not None:
        normalized["input_audio_noise_reduction"] = {
            "type": provider.sessionDefaults.noiseSuppression
        }
    if provider.sessionDefaults.echoCancellation is not None:
        normalized["input_audio_echo_cancellation"] = {
            "type": provider.sessionDefaults.echoCancellation
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
            logger.info("voice-live tool '%s' failed: %s", call.name, exc)
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
#
# Starlette runs on anyio, so the relay uses an anyio task group rather than raw
# asyncio tasks: when one direction ends it cancels the group's scope, and — just
# as importantly — the framework's own teardown cancellation (e.g. the client
# going away) stays anyio-native and is recognized by the enclosing cancel scope
# instead of leaking out and cancelling the whole request task.
# --------------------------------------------------------------------------- #


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
    rewrite_client_frame: Callable[[str], str],
    cancel_scope: anyio.CancelScope,
) -> None:
    try:
        while True:
            message = await client_ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is not None:
                # Inert (returns the frame unchanged) unless tools are enabled AND
                # this is a session.update, where the relay injects its tool set.
                await _send_upstream(upstream, lock, text=rewrite_client_frame(text))
                continue
            data = message.get("bytes")
            if data is not None:
                await _send_upstream(upstream, lock, data=data)
    finally:
        # The client side is done -> stop the upstream pump too.
        cancel_scope.cancel()


async def _pump_upstream_to_client(
    upstream: UpstreamConnection,
    client_ws: WebSocket,
    lock: anyio.Lock,
    bridge: ToolBridge,
    cancel_scope: anyio.CancelScope,
) -> None:
    try:
        while True:
            msg = await upstream.receive()
            if msg.kind == "close":
                return
            if msg.kind == "text" and msg.text is not None:
                await client_ws.send_text(msg.text)
                # Governed tool calling: a function-call event is executed in-process
                # and its result returned upstream. No-op (and no JSON parse) for
                # every other frame, and entirely skipped when tools are disabled.
                if bridge.enabled:
                    for frame in await bridge.handle_upstream_frame(msg.text):
                        await _send_upstream(upstream, lock, text=frame)
            elif msg.kind == "binary" and msg.data is not None:
                await client_ws.send_bytes(msg.data)
    finally:
        # The upstream side is done -> stop the client pump too.
        cancel_scope.cancel()


async def relay(
    client_ws: WebSocket,
    upstream: UpstreamConnection,
    *,
    max_seconds: float,
    bridge: ToolBridge,
    rewrite_client_frame: Callable[[str], str] | None = None,
) -> None:
    """Pump frames both ways until either side closes (or the optional clamp)."""

    send_lock = anyio.Lock()
    rewrite = rewrite_client_frame or bridge.rewrite_client_frame

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                _pump_client_to_upstream,
                client_ws,
                upstream,
                send_lock,
                rewrite,
                tg.cancel_scope,
            )
            tg.start_soon(
                _pump_upstream_to_client,
                upstream,
                client_ws,
                send_lock,
                bridge,
                tg.cancel_scope,
            )

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

    def rewrite_client_frame(frame: str) -> str:
        return bridge.rewrite_client_frame(provider_resolution.rewrite_client_frame(frame))

    try:
        async with connector.connect(
            url=url, headers=headers, timeout=settings.realtime_timeout_seconds
        ) as upstream:
            await state.usage.record_completion(
                user_id=user.internal_user_id,
                session_id=LIVE_SESSION_ID,
                model_id=provider_resolution.model_id,
                target=provider_resolution.usage_target,
                usage=_session_usage(),
                status="complete",
                correlation_id=correlation_id,
            )
            await relay(
                websocket,
                upstream,
                max_seconds=settings.realtime_max_session_seconds,
                bridge=bridge,
                rewrite_client_frame=rewrite_client_frame,
            )
    except Exception:  # noqa: BLE001 - any upstream/relay failure -> clean client close
        logger.warning(
            "voice-live relay error (provider=%s, target=%s, model=%s, correlation_id=%s)",
            provider_resolution.provider.id,
            provider_resolution.usage_target.target,
            provider_resolution.model_id,
            correlation_id,
            exc_info=True,
        )
        await _deny(websocket, WS_INTERNAL_ERROR)
        return

    await _deny(websocket, WS_NORMAL_CLOSURE)
