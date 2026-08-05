#!/usr/bin/env python3
"""Authenticated, operator-invoked canary for the governed Voice Live app path.

This script opens only the FastAPI ``/api/voice/live`` WebSocket. It never calls
APIM or a model endpoint directly, never acquires a token, and never sends audio.
The bearer token is read from an explicitly named environment variable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit

BEARER_SUBPROTOCOL = "ai4ia-bearer"
DEFAULT_TOKEN_ENV = "AI4IA_VOICE_CANARY_TOKEN"
VOICE_LIVE_PATH = "/api/voice/live"
MAX_SAFE_CHARS = 512
MAX_EVENT_FIELD_CHARS = 96

AZURE_OPENAI_PROVIDER = "azure_openai"
SPEECH_PROVIDER = "speech_voice_live"
PROVIDERS = (AZURE_OPENAI_PROVIDER, SPEECH_PROVIDER)

DEFAULT_INSTRUCTIONS = (
    "You are a helpful, concise voice assistant. Keep spoken replies brief and natural."
)
DEFAULT_AZURE_OPENAI_SESSION_UPDATE = (
    '{"type":"session.update","session":{"instructions":"You are a helpful, concise voice '
    'assistant. Keep spoken replies brief and natural.","voice":"alloy",'
    '"input_audio_format":"pcm16","output_audio_format":"pcm16",'
    '"turn_detection":{"type":"server_vad"},'
    '"input_audio_transcription":{"model":"whisper-1"}}}'
)

# Stable 2026-04-10 eastus2 managed-model contract. Keep this deliberately
# explicit: arbitrary Speech model names must fail before any socket is opened.
SPEECH_MODEL_TRANSCRIPTION = {
    "gpt-realtime": "gpt-4o-transcribe",
    "gpt-realtime-mini": "gpt-4o-transcribe",
    "gpt-4.1": "azure-speech",
    "gpt-4.1-mini": "azure-speech",
    "gpt-5-mini": "azure-speech",
    "gpt-5.1": "azure-speech",
}

SYNTHETIC_HISTORY = (
    ("user", "voice-canary-user"),
    ("assistant", "voice-canary-assistant"),
)
SEED_ITEM_ID_PREFIX = "voice-canary-seed-"
MAX_SEED_TURNS = 20
MAX_SEED_CHARS = 6000

_SELECTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SUBPROTOCOL_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_SAFE_EVENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|sig)=)[^&#\s]+",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    # `\"?` before the separator is load-bearing: in JSON the label's own closing
    # quote sits between the name and the colon (`"api_key": "..."`), and
    # `\s*[:=]` cannot cross it. This script's inputs are parsed frames rather
    # than raw bodies, so the shape is less likely here than in
    # `post-deploy-verify.py` -- but the same omission left a real gap in the
    # API's `redact()` for credentials under 32 characters, so the three copies
    # are kept consistent rather than each relying on its own inputs staying
    # narrow. If you unify these, unify toward the stricter pattern.
    r"(\b(?:api[_-]?key|subscription[_-]?key|access[_-]?token|token|authorization"
    r"|secret|password)\b\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
    re.IGNORECASE,
)


class CanaryInputError(ValueError):
    """The operator supplied an unsafe or unsupported canary input."""


class CanaryProtocolError(RuntimeError):
    def __init__(self, fields: dict[str, str | None], correlation: str | None) -> None:
        super().__init__("Voice Live returned a protocol error.")
        self.fields = fields
        self.correlation = correlation


class CanaryOrderError(RuntimeError):
    def __init__(self, message: str, correlation: str | None = None) -> None:
        super().__init__(message)
        self.correlation = correlation


class CanaryCloseError(RuntimeError):
    def __init__(
        self, code: int | None, reason: object, token: str, correlation: str | None
    ) -> None:
        super().__init__("Voice Live closed before the required acknowledgements.")
        self.code = code
        self.reason = sanitize(reason, secrets=(token,))
        self.correlation = correlation


class EventState:
    def __init__(self, expected_history_item_ids: Sequence[str] = ()) -> None:
        self.created = False
        self.updated = False
        self.correlation: str | None = None
        self.received_frames = 0
        self.expected_history_item_ids = tuple(expected_history_item_ids)
        self.acknowledged_history_item_ids: set[str] = set()


def compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def sanitize(
    value: object,
    *,
    max_chars: int = MAX_SAFE_CHARS,
    secrets: Sequence[str] = (),
) -> str | None:
    """Mirror the browser/API bounded metadata redaction without exposing frames."""

    if not isinstance(value, str):
        return None
    safe = value
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    safe = safe[:4096]
    safe = _CONTROL_RE.sub(" ", safe)
    safe = _BEARER_RE.sub("******", safe)
    safe = _JWT_RE.sub("[REDACTED]", safe)
    safe = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", safe)
    safe = _SECRET_VALUE_RE.sub(r"\1[REDACTED]", safe)
    safe = " ".join(safe.split()).strip()[: max(0, max_chars)]
    return safe or None


def _safe_selector(name: str, value: str | None, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise CanaryInputError(f"{name} is required.")
        return None
    candidate = value.strip()
    if not _SELECTOR_RE.fullmatch(candidate):
        raise CanaryInputError(f"{name} must be a simple catalog identifier.")
    return candidate


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise CanaryInputError("URL port is invalid.") from exc
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or not parsed.netloc.isascii()
        or not _HOST_RE.fullmatch(parsed.hostname)
        or _CONTROL_RE.search(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != VOICE_LIVE_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise CanaryInputError(
            "URL must be a query-free wss:// host ending exactly in /api/voice/live."
        )
    return urlunsplit(("wss", parsed.netloc, VOICE_LIVE_PATH, "", ""))


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise CanaryInputError("Origin port is invalid.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.netloc.isascii()
        or not _HOST_RE.fullmatch(parsed.hostname)
        or _CONTROL_RE.search(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise CanaryInputError("Origin must be a path-free https:// origin.")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def build_canary_url(
    base_url: str,
    *,
    provider: str,
    model: str,
    region: str | None = None,
    agent: str | None = None,
    tools: bool = False,
) -> str:
    base = validate_base_url(base_url)
    if provider not in PROVIDERS:
        raise CanaryInputError("Provider must be azure_openai or speech_voice_live.")
    selected_model = _safe_selector("model", model, required=True)
    selected_region = _safe_selector("region", region)
    selected_agent = _safe_selector("agent", agent)

    if provider == SPEECH_PROVIDER:
        if selected_region is not None:
            raise CanaryInputError("Region is allowed only for azure_openai.")
        if selected_model not in SPEECH_MODEL_TRANSCRIPTION:
            raise CanaryInputError("Speech model is not in the approved six-model allowlist.")

    query: list[tuple[str, str]] = [
        ("provider", provider),
        ("model", selected_model or ""),
    ]
    if selected_region is not None:
        query.append(("region", selected_region))
    if selected_agent is not None:
        query.append(("agent", selected_agent))
    if tools:
        query.append(("tools", "1"))
    parsed = urlsplit(base)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def speech_session_update(model: str) -> str:
    transcription = SPEECH_MODEL_TRANSCRIPTION.get(model)
    if transcription is None:
        raise CanaryInputError("Speech model is not in the approved six-model allowlist.")
    return compact_json(
        {
            "type": "session.update",
            "session": {
                "instructions": DEFAULT_INSTRUCTIONS,
                "voice": {
                    "type": "azure-standard",
                    "name": "en-US-Ava:DragonHDLatestNeural",
                    "locale": "en-US",
                },
                "input_audio_transcription": {
                    "model": transcription,
                    "language": "en-US",
                },
                "turn_detection": {
                    "type": "azure_semantic_vad",
                    "interrupt_response": True,
                    "auto_truncate": False,
                },
                "input_audio_noise_reduction": {
                    "type": "azure_deep_noise_suppression"
                },
                "input_audio_echo_cancellation": {
                    "type": "server_echo_cancellation"
                },
            },
        }
    )


def _selected_history(
    history: Sequence[tuple[str, str]] = SYNTHETIC_HISTORY,
) -> list[tuple[str, str]]:
    recent = [
        (role, text.strip())
        for role, text in history
        if role in ("user", "assistant") and isinstance(text, str) and text.strip()
    ][-MAX_SEED_TURNS:]
    selected: list[tuple[str, str]] = []
    budget = MAX_SEED_CHARS
    for role, text in reversed(recent):
        if budget <= 0:
            break
        bounded = text[:budget]
        selected.insert(0, (role, bounded))
        budget -= len(bounded)
    return selected


def expected_history_item_ids(
    history: Sequence[tuple[str, str]] = SYNTHETIC_HISTORY,
) -> tuple[str, ...]:
    return tuple(
        f"{SEED_ITEM_ID_PREFIX}{index:03d}"
        for index, _ in enumerate(_selected_history(history), start=1)
    )


def seed_frames(history: Sequence[tuple[str, str]] = SYNTHETIC_HISTORY) -> list[str]:
    selected = _selected_history(history)
    return [
        compact_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "id": f"{SEED_ITEM_ID_PREFIX}{index:03d}",
                    "type": "message",
                    "role": role,
                    "content": [
                        {
                            "type": "input_text" if role == "user" else "text",
                            "text": text,
                        }
                    ],
                },
            }
        )
        for index, (role, text) in enumerate(selected, start=1)
    ]


def build_initial_frames(provider: str, model: str) -> list[str]:
    if provider == AZURE_OPENAI_PROVIDER:
        _safe_selector("model", model, required=True)
        session = DEFAULT_AZURE_OPENAI_SESSION_UPDATE
    elif provider == SPEECH_PROVIDER:
        session = speech_session_update(model)
    else:
        raise CanaryInputError("Provider must be azure_openai or speech_voice_live.")
    return [session, *seed_frames()]


def _correlation_from(payload: dict[str, Any], token: str) -> str | None:
    for field in ("correlation_id", "correlationId", "request_id", "requestId"):
        value = sanitize(payload.get(field), max_chars=128, secrets=(token,))
        if value and _SAFE_EVENT_RE.fullmatch(value):
            return value
    return None


def inspect_event(frame: str, state: EventState, *, token: str) -> bool:
    """Return true after session setup and every synthetic seed acknowledgement."""

    state.received_frames += 1
    try:
        payload = json.loads(frame)
    except (TypeError, ValueError) as exc:
        raise CanaryOrderError("Received a non-JSON text event.", state.correlation) from exc
    if not isinstance(payload, dict):
        raise CanaryOrderError("Received a non-object text event.", state.correlation)
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not _SAFE_EVENT_RE.fullmatch(event_type):
        raise CanaryOrderError("Received an event without a safe type.", state.correlation)

    state.correlation = _correlation_from(payload, token) or state.correlation
    if event_type == "error":
        raw_error = payload.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        event_id = error.get("event_id")
        if not isinstance(event_id, str):
            event_id = payload.get("event_id")
        fields = {
            "type": sanitize(
                error.get("type"), max_chars=MAX_EVENT_FIELD_CHARS, secrets=(token,)
            ),
            "code": sanitize(
                error.get("code"), max_chars=MAX_EVENT_FIELD_CHARS, secrets=(token,)
            ),
            "param": sanitize(
                error.get("param"), max_chars=MAX_EVENT_FIELD_CHARS, secrets=(token,)
            ),
            "event_id": sanitize(
                event_id, max_chars=MAX_EVENT_FIELD_CHARS, secrets=(token,)
            ),
            "message": sanitize(error.get("message"), secrets=(token,))
            or "Live voice reported an error.",
        }
        raise CanaryProtocolError(fields, state.correlation)
    if event_type == "session.created":
        if state.updated:
            raise CanaryOrderError(
                "session.created arrived after session.updated.", state.correlation
            )
        state.created = True
    elif event_type == "session.updated":
        if not state.created:
            raise CanaryOrderError(
                "session.updated arrived before session.created.", state.correlation
            )
        state.updated = True
    elif event_type == "conversation.item.created":
        raw_item = payload.get("item")
        item = raw_item if isinstance(raw_item, dict) else {}
        item_id = item.get("id")
        if (
            isinstance(item_id, str)
            and item_id in state.expected_history_item_ids
        ):
            state.acknowledged_history_item_ids.add(item_id)
    return (
        state.created
        and state.updated
        and state.acknowledged_history_item_ids
        == set(state.expected_history_item_ids)
    )


def _emit(
    provider: str,
    model: str,
    outcome: str,
    *,
    correlation: str | None = None,
    fields: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "provider": provider,
        "model": model,
        "outcome": outcome,
    }
    if correlation:
        payload["correlation"] = correlation
    if fields:
        payload.update({key: value for key, value in fields.items() if value is not None})
    print(compact_json(payload))


async def run_canary(
    *,
    url: str,
    origin: str,
    provider: str,
    model: str,
    token: str,
    timeout: float,
) -> int:
    """Open the app relay, send canonical frames, and await ordered acknowledgements."""

    try:
        import aiohttp
    except ImportError:
        _emit(
            provider,
            model,
            "configuration_error",
            fields={"message": "aiohttp is required; install the app/api dependencies."},
        )
        return 2

    initial_frames = build_initial_frames(provider, model)
    state = EventState(expected_history_item_ids())
    ws: Any = None
    try:
        client_timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with asyncio.timeout(timeout):
                async with session.ws_connect(
                    url,
                    protocols=(BEARER_SUBPROTOCOL, token),
                    origin=origin,
                    autoping=True,
                    max_msg_size=1024 * 1024,
                ) as ws:
                    if ws.protocol != BEARER_SUBPROTOCOL:
                        raise CanaryOrderError(
                            "Server did not select the bearer auth subprotocol."
                        )
                    for frame in initial_frames:
                        await ws.send_str(frame)

                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            if inspect_event(message.data, state, token=token):
                                await ws.close(code=1000, message=b"canary-complete")
                                _emit(
                                    provider,
                                    model,
                                    "success",
                                    correlation=state.correlation,
                                )
                                return 0
                        elif message.type == aiohttp.WSMsgType.BINARY:
                            continue
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            raise CanaryCloseError(
                                ws.close_code,
                                getattr(message, "extra", None),
                                token,
                                state.correlation,
                            )
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise CanaryOrderError(
                                "WebSocket transport error.", state.correlation
                            )
                    raise CanaryCloseError(
                        ws.close_code, None, token, state.correlation
                    )
    except CanaryProtocolError as exc:
        _emit(
            provider,
            model,
            "protocol_error",
            correlation=exc.correlation,
            fields=exc.fields,
        )
        return 4
    except CanaryCloseError as exc:
        _emit(
            provider,
            model,
            "closed",
            correlation=exc.correlation,
            fields={
                "close_code": exc.code,
                "close_reason": exc.reason,
            },
        )
        return 5
    except CanaryOrderError as exc:
        _emit(
            provider,
            model,
            "order_error",
            correlation=exc.correlation,
            fields={"message": sanitize(str(exc), secrets=(token,))},
        )
        return 4
    except TimeoutError:
        if ws is not None and not ws.closed:
            await ws.close(code=1000, message=b"canary-timeout")
        _emit(
            provider,
            model,
            "timeout",
            correlation=state.correlation,
            fields={
                "message": (
                    "Timed out before session setup and every synthetic history "
                    "item were acknowledged."
                )
            },
        )
        return 3
    except Exception:
        # Handshake/transport exceptions can contain request headers or offered
        # subprotocols. Never stringify them.
        if ws is not None and not ws.closed:
            await ws.close(code=1000, message=b"canary-failed")
        _emit(
            provider,
            model,
            "connection_error",
            correlation=state.correlation,
            fields={"message": "Voice Live connection failed."},
        )
        return 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Canary the authenticated FastAPI /api/voice/live app path without audio."
        )
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Explicit query-free wss:// API URL ending in /api/voice/live.",
    )
    parser.add_argument(
        "--origin",
        required=True,
        help="Allowed https:// web origin to send in the WebSocket handshake.",
    )
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    parser.add_argument("--model", required=True, help="Provider model ID.")
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"Environment variable containing the Entra token (default: {DEFAULT_TOKEN_ENV}).",
    )
    parser.add_argument(
        "--region",
        help="Optional Azure OpenAI region. Rejected for speech_voice_live.",
    )
    parser.add_argument(
        "--agent",
        help="Optional governed agent name. Omitted by default.",
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="Request governed tools. Off by default.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Total handshake/event timeout in seconds (default: 20).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not _ENV_NAME_RE.fullmatch(args.token_env):
            raise CanaryInputError("Token environment variable name is invalid.")
        if not 1 <= args.timeout <= 120:
            raise CanaryInputError("Timeout must be between 1 and 120 seconds.")
        origin = validate_origin(args.origin)
        url = build_canary_url(
            args.url,
            provider=args.provider,
            model=args.model,
            region=args.region,
            agent=args.agent,
            tools=args.tools,
        )
        # Build before reading the token so unsupported Speech models are rejected
        # deterministically without any network or credential handling.
        build_initial_frames(args.provider, args.model)
    except CanaryInputError as exc:
        parser.error(str(exc))

    token = os.environ.get(args.token_env, "")
    if not token or not _SUBPROTOCOL_TOKEN_RE.fullmatch(token):
        _emit(
            args.provider,
            args.model,
            "configuration_error",
            fields={
                "message": (
                    f"{args.token_env} must contain a non-empty RFC-token-safe Entra token."
                )
            },
        )
        return 2
    return asyncio.run(
        run_canary(
            url=url,
            origin=origin,
            provider=args.provider,
            model=args.model,
            token=token,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
