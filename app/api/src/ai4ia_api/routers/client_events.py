"""Browser-side telemetry beacon.

The web app has no first-party client exception/event telemetry today: chat
render, microphone-capture, and TTS-playback failures are only visible to us
via Application Insights on the *backend* (:mod:`ai4ia_api.logging_setup`),
so a browser-only failure with a healthy upstream is invisible until a user
reports it. This router gives the browser a narrow, best-effort way to land
a small, bounded set of client-observed failures in that SAME customEvents
pipeline, instead of adding a new telemetry stack.

Deliberately minimal:
- No new telemetry SDK/dependency — reuses ``emit_custom_event`` exactly like
  chat completions, MCP tool calls, and document ingest already do.
- No free-form payloads: a small event-type enum plus short, length-capped
  text fields. No stack traces, no request/response bodies, no message
  content — content-free like ``emit_security_block``.
- No Cosmos write: this is ephemeral operational telemetry, not canonical
  domain data (see AGENTS.md "Cosmos is canonical" — this deliberately isn't
  that).
- Auth required, like every other non-health route, plus a tiny in-memory
  per-user rate limit so a runaway retry loop in one tab can't flood App
  Insights.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..logging_setup import emit_custom_event

router = APIRouter(prefix="/api/client-events", tags=["client-events"])

# Small, stable taxonomy so App Insights customEvents stay queryable — extend
# here rather than accepting free-form event names from the browser.
ClientEventType = Literal[
    "render_error",
    "unhandled_error",
    "unhandled_rejection",
    "media_playback_error",
    "microphone_error",
]

_MAX_MESSAGE_LEN = 300
_MAX_ROUTE_LEN = 200
_MAX_COMPONENT_LEN = 100

# Fixed-window per-user cap. In-memory/per-process is fine here — this is
# best-effort telemetry, not billing — generous enough for a genuinely broken
# session, tight enough that a runaway retry loop can't flood App Insights.
_RATE_LIMIT_PER_MINUTE = 20
_RATE_WINDOW_SECONDS = 60.0
_hits: dict[str, deque[float]] = {}


def _rate_limited(user_id: str) -> bool:
    now = time.monotonic()
    window = _hits.setdefault(user_id, deque())
    while window and now - window[0] > _RATE_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _RATE_LIMIT_PER_MINUTE:
        return True
    window.append(now)
    return False


class ClientEventReport(BaseModel):
    event: ClientEventType
    message: str = Field("", max_length=_MAX_MESSAGE_LEN)
    route: str = Field("", max_length=_MAX_ROUTE_LEN)
    component: str = Field("", max_length=_MAX_COMPONENT_LEN)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def report_client_event(
    body: ClientEventReport,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    # A misbehaving tab shouldn't loop retrying a telemetry beacon, so an
    # over-cap report is dropped silently (202) rather than erroring.
    if not _rate_limited(user.internal_user_id):
        emit_custom_event(
            "client_event",
            {
                "source": "browser",
                "event": body.event,
                "message": body.message or None,
                "route": body.route or None,
                "component": body.component or None,
                "userId": user.internal_user_id,
            },
        )
    return Response(status_code=status.HTTP_202_ACCEPTED)
