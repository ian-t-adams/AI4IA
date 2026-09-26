"""Live photo avatar sessions on the Speech Voice Live relay (Phase 2, default-off).

When the browser opens ``/api/voice/live?provider=speech_voice_live&avatar=<record id>``
the relay resolves one of the caller's own avatar records through
:func:`ai4ia_api.photo_avatars.live.resolve_live_avatar` and injects a
server-owned avatar block into every rebuilt ``session.update``. Voice Live then
streams the avatar as ``response.video.delta`` frames (base64 fragmented MP4:
H.264 video plus AAC audio) over the SAME governed WebSocket, FastAPI relay ->
APIM Voice Live API -> Foundry (``output_protocol: websocket``). There is no
WebRTC, no ICE/TURN credential and no direct browser media plane.

This module holds the IO-free parts so they can be tested without a socket:

* the server-owned avatar block and its injection;
* refusal of client ``session.avatar.*`` events (the WebRTC handshake);
* one decision per provider frame: a bounded, verbatim video forward, the
  provider avatar id scrubbed from every other frame (including the
  ``session.updated`` echo), confirmation, and a stable client error for
  ``avatar_verification_failed``;
* the idle tracker and the server-measured, per-second meter.

Video payloads are never parsed beyond their event type, logged, receipted or
copied into telemetry. The provider avatar id never reaches the browser, logs,
telemetry, receipts or usage rows: only an 8-character record-id prefix does.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from .photo_avatars.catalog import load_photo_avatar_catalog
from .photo_avatars.live import (
    LiveAvatarError, LiveAvatarGrant, live_cost_capped, resolve_live_avatar,
)
from .photo_avatars.provider import valid_provider_avatar_id
from .policy.models import PolicyError
from .usage.pricing import OperationCostEstimate, PricingBook, load_pricing

if TYPE_CHECKING:
    from .auth.base import AuthenticatedUser
    from .config import Settings

logger = logging.getLogger(__name__)

# Module-level clock so tests can drive the meter and idle tracker deterministically.
monotonic: Callable[[], float] = time.monotonic


def now() -> float:
    return monotonic()


AVATAR_QUERY_PARAM = "avatar"
_RECORD_ID = re.compile(r"[0-9a-f]{32}")
RECORD_REF_CHARS = 8
AVATAR_TYPE = "photo-avatar"
AVATAR_OUTPUT_PROTOCOL = "websocket"

SESSION_UPDATE_TYPE = "session.update"
VIDEO_DELTA_TYPE = "response.video.delta"
AUDIO_APPEND_TYPE = "input_audio_buffer.append"
SPEAKING_EVENT_TYPE = "session.avatar.switch_to_speaking"
IDLE_EVENT_TYPE = "session.avatar.switch_to_idle"
CLIENT_AVATAR_EVENT_PREFIX = "session.avatar."
VERIFICATION_FAILED_CODE = "avatar_verification_failed"
AVATAR_MODALITY = "avatar"

# Client events that only stop or clear output. The per-send policy guard lets
# them through without a fresh grant, so they never count as conversation: a
# client whose grant was revoked cannot keep an idle avatar streaming with them.
OUTPUT_STOP_EVENT_TYPES = frozenset({
    "response.cancel", "conversation.item.truncate", "input_audio_buffer.clear",
    "output_audio_buffer.clear",
})

# Every upstream text frame in an avatar session is bounded before it is parsed.
# The largest frame observed at 2026-04-10 was a 24,841-character video delta, so
# this leaves more than 10x headroom.
AVATAR_FRAME_MAX_CHARS = 256 * 1024
PROVIDER_ID_PLACEHOLDER = "[avatar]"
IDLE_TICK_SECONDS = 1.0
# While the avatar speaks its buffered answer only video arrives, so the idle
# countdown pauses. The pause is bounded: a speaking state that never ends
# resumes the countdown after this long (the session cap bounds it regardless).
SPEAKING_HOLD_MAX_SECONDS = 300.0
# How often the idle watchdog re-runs the session's policy guard, so a revoked
# grant ends a session even when the client sends nothing that is checked.
POLICY_RECHECK_SECONDS = 15.0
MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60

# Relay-originated events. The ``ai4ia.`` prefix never collides with provider events.
SESSION_EVENT = "ai4ia.avatar.session"
IDLE_WARNING_EVENT = "ai4ia.avatar.idle_warning"
SESSION_ENDED_EVENT = "ai4ia.avatar.session_ended"

AvatarErrorCode = Literal[
    "avatar_unavailable", "cost_unknown_under_cap", "avatar_connect_refused",
    "avatar_frame_too_large", "avatar_stream_refused",
]
AvatarUnavailableReason = Literal[
    # Layer 1's live refusal codes.
    "not_found", "not_ready", "needs_reverification", "home_changed", "policy_denied",
    # The availability predicate's reasons (``photo_avatars_unavailable``).
    "disabled", "storage_unavailable", "residency_unsupported", "policy_unavailable",
    "capability_unavailable", "capability_unknown",
    # Relay checks.
    "home_mismatch", "not_confirmed", "verification_failed", "unavailable",
]
AVATAR_UNAVAILABLE_REASONS: frozenset[str] = frozenset(get_args(AvatarUnavailableReason))
_PREDICATE_REASONS = frozenset({
    "disabled", "storage_unavailable", "residency_unsupported", "policy_unavailable",
    "capability_unavailable", "capability_unknown",
})
_LIVE_CODE_REASONS: dict[str, AvatarUnavailableReason] = {
    "not_found": "not_found",
    "avatar_not_ready": "not_ready",
    "avatar_needs_reverification": "needs_reverification",
    "avatar_home_changed": "home_changed",
    "policy_denied": "policy_denied",
}
_GENERIC_UNAVAILABLE = "Live avatars are unavailable right now."
UNAVAILABLE_MESSAGES: dict[str, str] = {
    "not_found": "This avatar no longer exists.",
    "not_ready": "This avatar isn't ready for live voice yet.",
    "needs_reverification": "The avatar service couldn't verify this avatar. Try again later.",
    "home_changed": "This avatar belongs to a different avatar home than live voice uses.",
    "home_mismatch": "This avatar belongs to a different avatar home than live voice uses.",
    "policy_denied": "Live avatars aren't permitted for your account.",
    "disabled": "Photo avatars are turned off.",
    "not_confirmed": "The avatar service didn't start this avatar, so the session ended.",
    "verification_failed": "The avatar service couldn't verify this avatar, so the session ended.",
}
StopReason = Literal[
    "avatar_frame_too_large", "avatar_verification_failed", "avatar_not_confirmed",
]
EndReason = Literal[
    "idle_timeout", "session_limit", "frame_too_large", "stream_refused",
    "verification_failed", "not_confirmed", "policy_revoked",
]


def valid_record_id(value: object) -> bool:
    return isinstance(value, str) and _RECORD_ID.fullmatch(value) is not None


def record_ref(record_id: str) -> str:
    """The short reference that may appear in logs, receipts and usage rows."""
    return record_id[:RECORD_REF_CHARS]


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def client_error(
    code: AvatarErrorCode, message: str, *, reason: str | None = None,
    retry_after: int | None = None,
) -> str:
    """A bounded, id-free error event in the provider's ``error`` event shape."""
    error: dict[str, Any] = {"type": "avatar_error", "code": code, "message": message}
    if reason is not None:
        error["reason"] = reason
    if retry_after is not None:
        error["retry_after_seconds"] = max(1, min(int(retry_after), MAX_RETRY_AFTER_SECONDS))
    return _dumps({"type": "error", "error": error})


def unavailable_error(reason: AvatarUnavailableReason, *, retry_after: int | None = None) -> str:
    return client_error(
        "avatar_unavailable", UNAVAILABLE_MESSAGES.get(reason, _GENERIC_UNAVAILABLE),
        reason=reason, retry_after=retry_after,
    )


def cost_unknown_error() -> str:
    return client_error(
        "cost_unknown_under_cap",
        "Live avatar time can't be priced, so it can't start while your usage has a spending cap.",
    )


def connect_refused_error() -> str:
    return client_error(
        "avatar_connect_refused", "This session doesn't accept WebRTC avatar connections.",
    )


def frame_too_large_error() -> str:
    return client_error(
        "avatar_frame_too_large",
        "The avatar video sent an oversized frame, so the session ended.",
    )


def stream_refused_error() -> str:
    return client_error(
        "avatar_stream_refused",
        "The avatar service sent data this session can't accept, so the session ended.",
    )


def refusal_reason(exc: LiveAvatarError) -> AvatarUnavailableReason:
    """Map a layer-1 live refusal to an allowlisted client reason."""
    if exc.code == "photo_avatars_unavailable":
        reason = exc.reason
        return reason if reason in _PREDICATE_REASONS else "unavailable"  # type: ignore[return-value]
    return _LIVE_CODE_REASONS.get(exc.code, "unavailable")


def refused_client_event(frame: str) -> bool:
    """True for any client ``session.avatar.*`` event, the WebRTC handshake included.

    Audio appends carry neither the literal prefix nor a JSON ``\\u`` escape, so
    they skip the parse; an escaped event type is decoded before it is judged.
    """
    if "session.avatar" not in frame and "\\u" not in frame:
        return False
    try:
        payload = json.loads(frame)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    kind = payload.get("type")
    return isinstance(kind, str) and kind.startswith(CLIENT_AVATAR_EVENT_PREFIX)


def client_frame_is_activity(event_type: str | None) -> bool:
    """Any client event except streaming microphone audio and output stop events."""
    return (
        event_type is not None
        and event_type != AUDIO_APPEND_TYPE
        and event_type not in OUTPUT_STOP_EVENT_TYPES
    )


def idle_warning_seconds(idle_timeout_seconds: float) -> int:
    return int(max(5, min(30, idle_timeout_seconds // 4)))


def effective_max_seconds(settings: Settings) -> float:
    """The avatar cap, tightened by ``realtime_max_session_seconds`` when that is set."""
    cap = float(settings.photo_avatar_live_max_minutes_per_session * 60)
    realtime_cap = settings.realtime_max_session_seconds
    if realtime_cap and realtime_cap > 0:
        cap = min(cap, float(realtime_cap))
    return cap


@dataclass(frozen=True, slots=True)
class UpstreamDecision:
    """What to do with one provider text frame in an avatar session.

    ``forward`` is sent to the browser (``None`` sends nothing). ``inspect`` is the
    id-scrubbed text the relay may read for its event-name and protocol-error
    metadata; it is ``None`` for video, which is counted but never inspected.
    """

    forward: str | None
    inspect: str | None
    video: bool = False
    stop: StopReason | None = None


@dataclass(frozen=True, slots=True)
class AvatarRefusal:
    """A connect-time refusal: one bounded client error, then a 1008 close."""

    frame: str
    security_reason: str


@dataclass(eq=False)
class LiveAvatarSession:
    """Per-connection avatar state. The provider id lives here and nowhere else."""

    record_id: str
    provider_avatar_id: str = field(repr=False)
    base_model: str
    home_region: str
    billing_model_id: str
    pricing: PricingBook = field(repr=False)
    max_seconds: float
    idle_timeout_seconds: float
    configured: bool = False
    confirmed_at: float | None = None
    ended_at: float | None = None
    last_activity: float = field(default_factory=now)
    idle_warned: bool = False
    # Set by switch_to_speaking, cleared by switch_to_idle.
    speaking_since: float | None = None
    last_policy_check: float = field(default_factory=now)
    video_frames: int = 0
    video_chars: int = 0
    max_video_frame_chars: int = 0
    verification_failed: bool = False
    end_reason: EndReason | None = None

    @property
    def record_ref(self) -> str:
        return record_ref(self.record_id)

    @property
    def idle_warning_seconds(self) -> int:
        return idle_warning_seconds(self.idle_timeout_seconds)

    def block(self) -> dict[str, Any]:
        """The only avatar configuration Voice Live ever receives from this relay."""
        return {
            "type": AVATAR_TYPE,
            "model": self.base_model,
            "character": self.provider_avatar_id,
            "customized": True,
            "output_protocol": AVATAR_OUTPUT_PROTOCOL,
        }

    def inject(self, frame: str) -> str:
        """Place the server-owned block into a rebuilt ``session.update``.

        Runs last in the rewrite chain, after Speech normalization (which already
        drops any client ``avatar``) and the tool/persona bridge, so nothing can
        override it. Every other frame is returned unchanged.
        """
        if SESSION_UPDATE_TYPE not in frame:
            return frame
        try:
            payload = json.loads(frame)
        except (TypeError, ValueError):
            return frame
        if not isinstance(payload, dict) or payload.get("type") != SESSION_UPDATE_TYPE:
            return frame
        session = payload.get("session")
        config = dict(session) if isinstance(session, dict) else {}
        config["avatar"] = self.block()
        payload["session"] = config
        self.configured = True
        return json.dumps(payload)

    def admission_payload(self) -> dict[str, Any]:
        return {
            "operation": "live_session",
            "avatar": self.record_ref,
            "baseModel": self.base_model,
            "outputProtocol": AVATAR_OUTPUT_PROTOCOL,
            "billingModelId": self.billing_model_id,
            "maxSeconds": math.ceil(self.max_seconds),
        }

    # --- provider frames --------------------------------------------------

    def upstream(self, text: str) -> UpstreamDecision:
        size = len(text)
        # The raw frame is bounded before anything parses it: no text frame above
        # the bound is parsed, inspected or forwarded in an avatar session.
        if size > AVATAR_FRAME_MAX_CHARS:
            self.end_reason = "frame_too_large"
            return UpstreamDecision(
                forward=frame_too_large_error(), inspect=None, stop="avatar_frame_too_large",
            )
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        event_type = payload.get("type") if isinstance(payload, dict) else None
        if event_type == VIDEO_DELTA_TYPE:
            self.video_frames += 1
            self.video_chars += size
            self.max_video_frame_chars = max(self.max_video_frame_chars, size)
            self._confirm()
            return UpstreamDecision(forward=text, inspect=None, video=True)

        # Every other provider event is conversational activity.
        self.touch()
        if event_type == SPEAKING_EVENT_TYPE:
            self.speaking_since = now()
        elif event_type == IDLE_EVENT_TYPE:
            self.speaking_since = None
        scrubbed = self.scrub(text)
        if scrubbed is not text:
            try:
                payload = json.loads(scrubbed)
            except (TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            return UpstreamDecision(forward=scrubbed, inspect=scrubbed)

        if event_type in ("session.created", "session.updated"):
            session = payload.get("session")
            confirmed = event_type == "session.updated" and _confirms_avatar(session)
            forward = scrubbed
            if isinstance(session, dict) and "avatar" in session:
                projected = dict(session)
                projected["avatar"] = _safe_avatar_echo(session.get("avatar"))
                forward = _dumps({**payload, "session": projected})
            if confirmed:
                self._confirm()
            elif event_type == "session.updated" and self.configured and self.confirmed_at is None:
                self.end_reason = "not_confirmed"
                return UpstreamDecision(
                    forward=unavailable_error("not_confirmed"), inspect=forward,
                    stop="avatar_not_confirmed",
                )
            return UpstreamDecision(forward=forward, inspect=forward)

        if event_type == "error":
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code == VERIFICATION_FAILED_CODE:
                self.verification_failed = True
                self.end_reason = "verification_failed"
                return UpstreamDecision(
                    forward=unavailable_error("verification_failed"), inspect=scrubbed,
                    stop="avatar_verification_failed",
                )
        return UpstreamDecision(forward=scrubbed, inspect=scrubbed)

    def scrub(self, text: str) -> str:
        """Replace the provider avatar id in any provider-derived text.

        Provider ids are ``ai4ia-`` + hex, so they can never occur inside
        standard base64 video. Every other frame, close reason and error message
        is scrubbed before it is forwarded, inspected or logged.
        """
        if self.provider_avatar_id and self.provider_avatar_id in text:
            return text.replace(self.provider_avatar_id, PROVIDER_ID_PLACEHOLDER)
        return text

    def scrub_optional(self, text: str | None) -> str | None:
        return None if text is None else self.scrub(text)

    def _confirm(self) -> None:
        if self.confirmed_at is None:
            self.confirmed_at = now()

    # --- idle and meter ---------------------------------------------------

    def touch(self) -> None:
        self.last_activity = now()
        self.idle_warned = False

    def idle_check(self) -> tuple[Literal["warn", "timeout"] | None, int]:
        current = now()
        reference = self.last_activity
        if self.speaking_since is not None:
            # The avatar is still speaking its answer, which is conversation even
            # though only video arrives. Hold the countdown, but only for a bounded
            # time, so a speaking state that never ends cannot hold the session open.
            reference = max(reference, min(current, self.speaking_since + SPEAKING_HOLD_MAX_SECONDS))
        remaining = self.idle_timeout_seconds - (current - reference)
        if remaining <= 0:
            self.end_reason = "idle_timeout"
            return "timeout", 0
        seconds = max(1, math.ceil(remaining))
        if remaining <= self.idle_warning_seconds and not self.idle_warned:
            self.idle_warned = True
            return "warn", seconds
        return None, seconds

    def policy_recheck_due(self) -> bool:
        """True at most once per ``POLICY_RECHECK_SECONDS``."""
        current = now()
        if current - self.last_policy_check < POLICY_RECHECK_SECONDS:
            return False
        self.last_policy_check = current
        return True

    def finish(self) -> None:
        if self.ended_at is None:
            self.ended_at = now()

    @property
    def billable_seconds(self) -> int:
        """Whole seconds from avatar confirmation to close; none if never confirmed."""
        if self.confirmed_at is None:
            return 0
        end = self.ended_at if self.ended_at is not None else now()
        return max(1, math.ceil(max(0.0, end - self.confirmed_at)))

    def cost(self) -> OperationCostEstimate | None:
        seconds = self.billable_seconds
        if seconds <= 0:
            return None
        return self.pricing.estimate_avatar_seconds(self.billing_model_id, seconds=seconds)

    def evidence(self) -> dict[str, Any]:
        """Content-free facts for the completion log and custom event."""
        cost = self.cost()
        return {
            "recordRef": self.record_ref,
            "baseModel": self.base_model,
            "outputProtocol": AVATAR_OUTPUT_PROTOCOL,
            "configured": self.configured,
            "confirmed": self.confirmed_at is not None,
            "billableSeconds": self.billable_seconds,
            "billingModelId": self.billing_model_id,
            "costKnown": bool(cost is not None and cost.known),
            "estCostMicroUsd": cost.micro_usd if cost is not None and cost.known else None,
            "priceVersion": cost.version if cost is not None else self.pricing.version,
            "videoFrames": self.video_frames,
            "videoChars": self.video_chars,
            "maxVideoFrameChars": self.max_video_frame_chars,
            "verificationFailed": self.verification_failed,
            "endReason": self.end_reason,
        }

    # --- relay events -----------------------------------------------------

    def session_event(self) -> str:
        return _dumps({
            "type": SESSION_EVENT,
            "output_protocol": AVATAR_OUTPUT_PROTOCOL,
            "idle_timeout_seconds": math.ceil(self.idle_timeout_seconds),
            "idle_warning_seconds": self.idle_warning_seconds,
            "max_session_seconds": math.ceil(self.max_seconds),
        })

    @staticmethod
    def idle_warning_event(seconds_remaining: int) -> str:
        return _dumps({"type": IDLE_WARNING_EVENT, "seconds_remaining": seconds_remaining})

    def ended_event(self) -> str | None:
        """A friendly, non-error notice for a governed end (idle or cap), else None."""
        if self.end_reason not in ("idle_timeout", "session_limit"):
            return None
        return _dumps({"type": SESSION_ENDED_EVENT, "reason": self.end_reason})


def _confirms_avatar(session: object) -> bool:
    if not isinstance(session, dict):
        return False
    modalities = session.get("modalities")
    if isinstance(modalities, list) and AVATAR_MODALITY in modalities:
        return True
    avatar = session.get("avatar")
    return isinstance(avatar, dict) and avatar.get("output_protocol") == AVATAR_OUTPUT_PROTOCOL


def _safe_avatar_echo(avatar: object) -> dict[str, str] | None:
    """Only the avatar echo fields the browser may see: no character, ICE or media URLs."""
    if not isinstance(avatar, dict):
        return None
    return {
        key: value for key in ("type", "output_protocol")
        if isinstance(value := avatar.get(key), str)
    }


async def open_live_avatar(
    state: Any,
    settings: Settings,
    user: AuthenticatedUser,
    record_id: str,
    *,
    session_region: str | None,
) -> LiveAvatarSession | AvatarRefusal:
    """Resolve, place and price one live avatar before any admission or upstream.

    Re-runs layer 1's grant (ownership, readiness, re-verification, home and the
    availability predicate with ``avatar.use`` enforced) on every connection, then
    requires the Voice Live target region to own the avatar and refuses an
    unpriced meter under a cost cap. Never falls back to audio-only on its own.
    """
    try:
        grant: LiveAvatarGrant = await resolve_live_avatar(state, user, record_id)
    except LiveAvatarError as exc:
        reason = refusal_reason(exc)
        return AvatarRefusal(
            unavailable_error(reason, retry_after=exc.retry_after), f"avatar_{reason}",
        )
    except PolicyError:
        return AvatarRefusal(unavailable_error("policy_unavailable"), "avatar_policy_unavailable")
    except Exception as exc:  # noqa: BLE001 - never a 500 during the handshake
        logger.warning("voice-live avatar resolution failed (%s)", type(exc).__name__)
        return AvatarRefusal(unavailable_error("unavailable"), "avatar_unavailable")
    if session_region is None or grant.home_region != session_region:
        return AvatarRefusal(unavailable_error("home_mismatch"), "avatar_home_mismatch")
    if not valid_record_id(grant.record_id) or not valid_provider_avatar_id(
        grant.provider_avatar_id,
    ):
        return AvatarRefusal(unavailable_error("unavailable"), "avatar_unavailable")
    billing_model_id = load_photo_avatar_catalog().liveBillingModelId
    usage = getattr(state, "usage", None)
    pricing = getattr(usage, "pricing", None)
    if not isinstance(pricing, PricingBook):
        pricing = load_pricing()
    # Snapshot the rate before the provider await; unknown is never free.
    if not pricing.estimate_avatar_seconds(billing_model_id, seconds=1).known:
        try:
            capped = await live_cost_capped(state, user)
        except LiveAvatarError as exc:
            reason = refusal_reason(exc)
            return AvatarRefusal(unavailable_error(reason), f"avatar_{reason}")
        except PolicyError:
            return AvatarRefusal(
                unavailable_error("policy_unavailable"), "avatar_policy_unavailable",
            )
        if capped:
            return AvatarRefusal(cost_unknown_error(), "avatar_cost_unknown_under_cap")
    return LiveAvatarSession(
        record_id=grant.record_id,
        provider_avatar_id=grant.provider_avatar_id,
        base_model=grant.base_model,
        home_region=grant.home_region,
        billing_model_id=billing_model_id,
        pricing=pricing,
        max_seconds=effective_max_seconds(settings),
        idle_timeout_seconds=float(settings.photo_avatar_live_idle_timeout_seconds),
    )
