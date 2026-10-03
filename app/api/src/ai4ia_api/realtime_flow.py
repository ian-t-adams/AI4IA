"""Content-free conversation-flow counts for one live voice session.

The relay already names each text frame's event type. This module turns those
names into bounded per-session counts for a fixed allowlist of turn-taking
events in each direction, plus a histogram of how responses ended. It reads
nothing else: no transcript, item or response id, token count, provider avatar
id, URL or free text. Only ``response.done`` payloads are parsed for their
status, and only below a size bound; audio and video deltas never are.

Version 2 adds why the session's first failed response failed and two facts
about the session configuration the relay itself owns. From the failure it keeps
only the ``type``, ``code`` and ``param`` of ``response.status_details.error``,
each as a short lowercase identifier or the literal ``other``, never its message.
The configuration facts are the server-owned reasoning effort the relay set, and
how many client temperatures it omitted for a model without sampling.

Every key is a fixed string from this module, so the counts are bounded by the
allowlists however many frames a session sends, and every string value is a
bounded identifier.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

FLOW_TELEMETRY_VERSION = 2

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

# The first failed response's ``status_details.error`` fields, by the
# custom-event property that carries each. Its message is never read.
RESPONSE_FAILURE_FIELDS: Mapping[str, str] = {
    "type": "responseFailedType",
    "code": "responseFailedCode",
    "param": "responseFailedParam",
}
# What a recorded identifier may look like: an enum value such as
# ``invalid_request_error`` or a parameter path such as ``session.temperature``.
# fullmatch, so a trailing newline is not an identifier either.
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
OTHER_IDENTIFIER = "other"

# Session configuration the relay owns, by custom-event property: the
# reasoning effort it set from the catalog, and how many client temperatures it
# omitted because the managed model has no sampling.
REASONING_EFFORT_PROPERTY = "reasoningEffort"
TEMPERATURE_OMITTED_PROPERTY = "clientTemperatureOmitted"


def bounded_identifier(value: object) -> str:
    """``value`` when it is a short lowercase identifier, otherwise ``other``."""
    if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
        return value
    return OTHER_IDENTIFIER


@dataclass(frozen=True, slots=True)
class ResponseDone:
    """How one ``response.done`` ended, plus a failed response's error identifiers."""

    outcome: str
    # (field, identifier) pairs in RESPONSE_FAILURE_FIELDS order; failed only.
    failure: tuple[tuple[str, str], ...] = ()


def failure_identifiers(error: object) -> tuple[tuple[str, str], ...]:
    """The bounded ``type``/``code``/``param`` of a failed response's error.

    An absent or null field is omitted; any other value that is not an
    identifier becomes ``other``. Nothing else in the error is read.
    """
    if not isinstance(error, dict):
        return ()
    return tuple(
        (field, bounded_identifier(error[field]))
        for field in RESPONSE_FAILURE_FIELDS
        if error.get(field) is not None
    )


def parse_response_done(frame: str) -> ResponseDone:
    """The bounded outcome of one ``response.done`` frame, parsed once."""
    if len(frame) > RESPONSE_DONE_MAX_CHARS:
        return ResponseDone("other")
    try:
        payload = json.loads(frame)
    except (TypeError, ValueError, RecursionError):
        return ResponseDone("other")
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        return ResponseDone("other")
    status = response.get("status")
    details = response.get("status_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if not isinstance(status, str):
        return ResponseDone("other")
    if not isinstance(reason, str):
        reason = None
    if status == "completed":
        return ResponseDone("completed")
    if status == "failed":
        error = details.get("error") if isinstance(details, dict) else None
        return ResponseDone("failed", failure_identifiers(error))
    if status == "cancelled":
        return ResponseDone(
            f"cancelled:{reason}" if reason in _CANCELLED_REASONS else "cancelled:other"
        )
    if status == "incomplete":
        return ResponseDone(
            f"incomplete:{reason}" if reason in _INCOMPLETE_REASONS else "incomplete:other"
        )
    return ResponseDone("other")


def response_outcome(frame: str) -> str:
    """The bounded outcome label for one ``response.done`` frame."""
    return parse_response_done(frame).outcome


def ordered_counts(counts: Mapping[str, int], allowlist: Iterable[str]) -> tuple[tuple[str, int], ...]:
    """Non-zero counts in allowlist order, so identical sessions log identically."""
    return tuple((key, counts[key]) for key in allowlist if counts.get(key, 0) > 0)


def event_properties(
    client: Iterable[tuple[str, int]],
    upstream: Iterable[tuple[str, int]],
    outcomes: Iterable[tuple[str, int]],
    *,
    failure: Iterable[tuple[str, str]] = (),
    reasoning_effort: str | None = None,
    temperature_omitted: int = 0,
) -> dict[str, int | str]:
    """Flat custom-event properties; a count absent from a versioned event is zero.

    Counts are numbers. The first failure's fields and the reasoning effort are
    bounded identifiers, each present only when there was one to record.
    """
    properties: dict[str, int | str] = {"flowVersion": FLOW_TELEMETRY_VERSION}
    for pairs, names in (
        (client, CLIENT_FLOW_EVENTS),
        (upstream, UPSTREAM_FLOW_EVENTS),
        (outcomes, RESPONSE_OUTCOMES),
    ):
        for key, count in pairs:
            name = names.get(key)
            if name is not None and count > 0:
                properties[name] = count
    for field, identifier in failure:
        name = RESPONSE_FAILURE_FIELDS.get(field)
        if name is not None:
            properties[name] = bounded_identifier(identifier)
    if reasoning_effort is not None:
        properties[REASONING_EFFORT_PROPERTY] = bounded_identifier(reasoning_effort)
    if temperature_omitted > 0:
        properties[TEMPERATURE_OMITTED_PROPERTY] = temperature_omitted
    return properties
