"""Content-free conversation-flow counts for one live voice session.

The relay already names each text frame's event type. This module turns those
names into bounded per-session counts for a fixed allowlist of turn-taking
events in each direction, plus a histogram of how responses ended. It reads
nothing else: no transcript, item or response id, token count, provider avatar
id, URL or free text. Only ``response.done`` payloads are parsed for their
status, and only below a size bound; audio and video deltas never are.

Every key is a fixed string from this module, so the counts are bounded by the
allowlists however many frames a session sends.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

FLOW_TELEMETRY_VERSION = 1

# Browser -> relay events, by the name the browser sent (before any rewrite),
# mapped to the custom-event property that carries the count.
CLIENT_FLOW_EVENTS: Mapping[str, str] = {
    "session.update": "clientSessionUpdate",
    "input_audio_buffer.append": "clientAudioAppend",
    "input_audio_buffer.commit": "clientAudioCommit",
    "input_audio_buffer.clear": "clientAudioClear",
    "conversation.item.create": "clientItemCreate",
    "conversation.item.truncate": "clientItemTruncate",
    "conversation.item.delete": "clientItemDelete",
    "response.create": "clientResponseCreate",
    "response.cancel": "clientResponseCancel",
    "output_audio_buffer.clear": "clientOutputAudioClear",
}

# Provider -> relay events, by the name the provider sent.
UPSTREAM_FLOW_EVENTS: Mapping[str, str] = {
    "session.created": "upSessionCreated",
    "session.updated": "upSessionUpdated",
    "input_audio_buffer.speech_started": "upSpeechStarted",
    "input_audio_buffer.speech_stopped": "upSpeechStopped",
    "input_audio_buffer.committed": "upAudioCommitted",
    "input_audio_buffer.cleared": "upAudioCleared",
    "conversation.item.input_audio_transcription.completed": "upTranscriptionCompleted",
    "conversation.item.input_audio_transcription.failed": "upTranscriptionFailed",
    "response.created": "upResponseCreated",
    "response.done": "upResponseDone",
    "response.function_call_arguments.done": "upFunctionCallDone",
    "output_audio_buffer.started": "upOutputAudioStarted",
    "output_audio_buffer.stopped": "upOutputAudioStopped",
    "output_audio_buffer.cleared": "upOutputAudioCleared",
    "session.avatar.switch_to_speaking": "upAvatarSpeaking",
    "session.avatar.switch_to_idle": "upAvatarIdle",
    "error": "upError",
}

RESPONSE_DONE_TYPE = "response.done"
# A response.done carries the whole response, so its size grows with the reply.
# Larger ones are counted as ``other`` rather than parsed.
RESPONSE_DONE_MAX_CHARS = 256 * 1024

# How a response ended: the documented status and status_details.reason enums.
# Anything else, including a malformed or oversized payload, is ``other``.
RESPONSE_OUTCOMES: Mapping[str, str] = {
    "completed": "responseCompleted",
    "cancelled:turn_detected": "responseCancelledTurnDetected",
    "cancelled:client_cancelled": "responseCancelledClientCancelled",
    "cancelled:other": "responseCancelledOther",
    "incomplete:max_output_tokens": "responseIncompleteMaxOutputTokens",
    "incomplete:content_filter": "responseIncompleteContentFilter",
    "incomplete:other": "responseIncompleteOther",
    "failed": "responseFailed",
    "other": "responseOther",
}
_CANCELLED_REASONS = frozenset({"turn_detected", "client_cancelled"})
_INCOMPLETE_REASONS = frozenset({"max_output_tokens", "content_filter"})


def response_outcome(frame: str) -> str:
    """The bounded outcome label for one ``response.done`` frame."""
    if len(frame) > RESPONSE_DONE_MAX_CHARS:
        return "other"
    try:
        payload = json.loads(frame)
    except (TypeError, ValueError, RecursionError):
        return "other"
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        return "other"
    status = response.get("status")
    details = response.get("status_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if not isinstance(status, str):
        return "other"
    if not isinstance(reason, str):
        reason = None
    if status in ("completed", "failed"):
        return status
    if status == "cancelled":
        return f"cancelled:{reason}" if reason in _CANCELLED_REASONS else "cancelled:other"
    if status == "incomplete":
        return f"incomplete:{reason}" if reason in _INCOMPLETE_REASONS else "incomplete:other"
    return "other"


def ordered_counts(counts: Mapping[str, int], allowlist: Iterable[str]) -> tuple[tuple[str, int], ...]:
    """Non-zero counts in allowlist order, so identical sessions log identically."""
    return tuple((key, counts[key]) for key in allowlist if counts.get(key, 0) > 0)


def event_properties(
    client: Iterable[tuple[str, int]],
    upstream: Iterable[tuple[str, int]],
    outcomes: Iterable[tuple[str, int]],
) -> dict[str, int]:
    """Flat numeric custom-event properties; a count absent from a v1 event is zero."""
    properties: dict[str, int] = {"flowVersion": FLOW_TELEMETRY_VERSION}
    for pairs, names in (
        (client, CLIENT_FLOW_EVENTS),
        (upstream, UPSTREAM_FLOW_EVENTS),
        (outcomes, RESPONSE_OUTCOMES),
    ):
        for key, count in pairs:
            name = names.get(key)
            if name is not None and count > 0:
                properties[name] = count
    return properties
