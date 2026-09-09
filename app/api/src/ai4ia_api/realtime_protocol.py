"""Azure OpenAI wire adapters for the relay's stable, preview-shaped contract.

GA schema: Microsoft Learn's realtime-audio-preview-api-migration-guide and the
OpenAI generated realtime types at 41f0a2317759e8796ccfbde75536bd42e4aca7a2.
Only protocol envelopes are translated; tool arguments, audio bytes, usage,
errors, cancellation identifiers, and unknown events are never recursively rewritten.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any


class RealtimeProtocol(str, Enum):
    preview = "preview"
    ga = "ga"


class RealtimeProtocolError(ValueError):
    """An invalid application-protocol frame; messages never contain client data."""


_GA_EVENT_NAMES = {
    f"response.output_{kind}.{suffix}": f"response.{kind}.{suffix}"
    for kind in ("text", "audio", "audio_transcript")
    for suffix in ("delta", "done")
}
_GA_EVENT_NAMES["conversation.item.added"] = "conversation.item.created"
_INPUT_AUDIO_FIELDS = {
    "input_audio_format": "format",
    "input_audio_transcription": "transcription",
    "input_audio_noise_reduction": "noise_reduction",
    "turn_detection": "turn_detection",
}
_OUTPUT_AUDIO_FIELDS = {
    "output_audio_format": "format",
    "voice": "voice",
    "speed": "speed",
}
_AUDIO_FORMATS = {
    "pcm16": {"type": "audio/pcm", "rate": 24000},
    "g711_ulaw": {"type": "audio/pcmu"},
    "g711_alaw": {"type": "audio/pcma"},
}


def _event(frame: str, *, strict: bool = False) -> dict[str, Any] | None:
    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload
    if strict:
        raise RealtimeProtocolError("GA realtime requires a JSON event object.")
    return None


def _ga_format(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or value not in _AUDIO_FORMATS:
        raise RealtimeProtocolError("Unsupported realtime audio format.")
    return dict(_AUDIO_FORMATS[value])


def _legacy_format(value: object) -> object:
    for name, shape in _AUDIO_FORMATS.items():
        if value == shape:
            return name
    # An unrecognized upstream format must remain visible, not masquerade as PCM.
    return value


def _ga_modalities(value: object) -> list[str]:
    if not isinstance(value, list) or not value or any(
        modality not in ("text", "audio") for modality in value
    ):
        raise RealtimeProtocolError("Unsupported realtime output modalities.")
    # GA audio includes a transcript; requesting both modalities is invalid.
    return ["audio"] if "audio" in value else ["text"]


def _content(part: object, *, to_ga: bool) -> object:
    if not isinstance(part, dict):
        return part
    names = (
        {"text": "output_text", "audio": "output_audio"}
        if to_ga else {"output_text": "text", "output_audio": "audio"}
    )
    kind = part.get("type")
    if isinstance(kind, str) and kind in names:
        return {**part, "type": names[kind]}
    return part


def _item(item: object, *, to_ga: bool) -> object:
    if (
        not isinstance(item, dict)
        or item.get("type") != "message"
        or item.get("role") != "assistant"
        or not isinstance(item.get("content"), list)
    ):
        return item
    return {**item, "content": [_content(part, to_ga=to_ga) for part in item["content"]]}


def _ga_config(config: dict[str, Any], *, session: bool) -> dict[str, Any]:
    # Browser/native shapes must not compete. The app always speaks the existing
    # flat contract; accepting nested overrides would bypass its audio controls.
    if "audio" in config or "output_modalities" in config or (
        session and "max_output_tokens" in config
    ):
        raise RealtimeProtocolError("Use the relay's flat realtime configuration.")
    result = dict(config)
    result.pop("temperature", None)  # Removed from the GA schema; no equivalent.
    if "modalities" in result:
        result["output_modalities"] = _ga_modalities(result.pop("modalities"))
    if session and "max_response_output_tokens" in result:
        result["max_output_tokens"] = result.pop("max_response_output_tokens")
    audio: dict[str, Any] = {}
    for side, fields in (
        ("input", _INPUT_AUDIO_FIELDS if session else {}),
        ("output", _OUTPUT_AUDIO_FIELDS),
    ):
        values: dict[str, Any] = {}
        for old, new in fields.items():
            if old in result:
                value = result.pop(old)
                values[new] = _ga_format(value) if new == "format" else value
        if values:
            audio[side] = values
    if audio:
        result["audio"] = audio
    if not session and isinstance(result.get("input"), list):
        result["input"] = [_item(item, to_ga=True) for item in result["input"]]
    return result


def rewrite_openai_client_frame(
    frame: str, *, protocol: RealtimeProtocol, deployment: str
) -> str:
    """Translate before the shared tool/persona rewrite, fixing the target here."""
    ga = protocol == RealtimeProtocol.ga
    payload = _event(frame, strict=ga)
    if payload is None:
        return frame
    result = dict(payload)
    event_type = payload["type"]
    if event_type in ("session.update", "response.create"):
        field = "session" if event_type == "session.update" else "response"
        config = payload.get(field)
        if config is not None and not isinstance(config, dict):
            if ga:
                raise RealtimeProtocolError("Realtime configuration must be an object.")
            return frame
        updated = dict(config or {})
        # The handshake's catalog resolution is authoritative. Preview cannot
        # update a model; GA accepts it, but never from the browser.
        updated.pop("model", None)
        updated.pop("deployment", None)
        if ga:
            updated = _ga_config(updated, session=field == "session")
            if field == "session":
                updated["type"] = "realtime"
                updated["model"] = deployment
        elif field == "session":
            updated.pop("type", None)
        if config is not None or updated:
            result[field] = updated
    elif ga and event_type == "conversation.item.create" and "item" in payload:
        result["item"] = _item(payload["item"], to_ga=True)
    return frame if result == payload else json.dumps(result)


def _legacy_config(config: dict[str, Any], *, session: bool) -> dict[str, Any]:
    result = dict(config)
    if session and result.get("type") == "realtime":
        result.pop("type")
    if "output_modalities" in result:
        modalities = result.pop("output_modalities")
        result["modalities"] = ["text", "audio"] if modalities == ["audio"] else modalities
    if session and "max_output_tokens" in result:
        result["max_response_output_tokens"] = result.pop("max_output_tokens")
    audio = result.get("audio")
    if isinstance(audio, dict):
        remaining = dict(audio)
        for side, fields in (
            ("input", _INPUT_AUDIO_FIELDS if session else {}),
            ("output", _OUTPUT_AUDIO_FIELDS),
        ):
            values = remaining.get(side)
            if not isinstance(values, dict):
                continue
            values = dict(values)
            for old, new in fields.items():
                if new in values:
                    value = values.pop(new)
                    result[old] = _legacy_format(value) if new == "format" else value
            if values:
                remaining[side] = values
            else:
                remaining.pop(side)
        if remaining:
            result["audio"] = remaining
        else:
            result.pop("audio")
    return result


def rewrite_ga_upstream_frame(frame: str) -> str:
    """Normalize GA events once, before browser playback and transcript consumers."""
    payload = _event(frame)
    if payload is None:
        return frame
    result = dict(payload)
    event_type = payload["type"]
    if event_type in _GA_EVENT_NAMES:
        result["type"] = _GA_EVENT_NAMES[event_type]
    if event_type in ("session.created", "session.updated"):
        session = payload.get("session")
        if isinstance(session, dict):
            result["session"] = _legacy_config(session, session=True)
    elif event_type in ("response.created", "response.done"):
        response = payload.get("response")
        if isinstance(response, dict):
            response = _legacy_config(response, session=False)
            if isinstance(response.get("output"), list):
                response["output"] = [_item(item, to_ga=False) for item in response["output"]]
            result["response"] = response
    elif event_type in (
        "conversation.item.added", "conversation.item.done", "conversation.item.retrieved",
        "response.output_item.added", "response.output_item.done",
    ):
        if "item" in payload:
            result["item"] = _item(payload["item"], to_ga=False)
    elif event_type in ("response.content_part.added", "response.content_part.done"):
        if "part" in payload:
            result["part"] = _content(payload["part"], to_ga=False)
    return frame if result == payload else json.dumps(result)
