"""Server-authoritative request priority for SimpleL7Proxy queue fairness.

SimpleL7Proxy keeps a per-replica priority queue and can reserve workers per
priority band (``PriorityWorker``). It reads the band from an inbound header —
``x-S7PPriority`` — and, per ``simplel7proxy_inbound_post_32.xml``, falls back to
the **lowest** band when the header is absent or unparseable. Nothing in FastAPI
set that header before, so every request landed in the lowest band and the
reservation feature had no observable effect.

**This value is derived here and nowhere else.** It is computed from the
already-authenticated principal and stashed in a ContextVar that only
``ai4ia_api.auth.dependencies.get_current_user`` writes. The gateway client reads
it when building outbound headers. A browser cannot influence it: ``apiFetch``
talks to FastAPI, FastAPI holds the proxy-ingress key, and the client never
copies an inbound ``x-S7PPriority`` onto the outbound request. Treating a
client-supplied band as authoritative would be a privilege escalation — any user
could claim the reserved workers — which is why resolution takes an
``AuthenticatedUser`` and not a request.

A ContextVar is the right carrier because priority is per-request but the gateway
client is a long-lived, settings-scoped singleton; threading an extra argument
through every chat/embed/image/audio call site would touch far more surface for
the same result. asyncio copies the context per task, so concurrent requests
cannot read each other's band.
"""
from __future__ import annotations

import contextvars

from ..auth.base import AuthenticatedUser
from ..auth.identity import identity_is_admin
from ..config import Settings

# Bands must match PriorityKeys/PriorityValues in infra/modules/gateway.bicep:
#   ['high', 'standard', 'batch'] -> [1, 2, 3]
# Lower is better. PRIORITY_BATCH equals the proxy's own no-header default, so
# emitting it is equivalent to emitting nothing.
PRIORITY_HIGH = 1
PRIORITY_STANDARD = 2
PRIORITY_BATCH = 3

PRIORITY_HEADER = "x-S7PPriority"

_request_priority: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "ai4ia_request_priority", default=None
)


def resolve_priority(user: AuthenticatedUser, settings: Settings) -> int:
    """Map an authenticated principal to a proxy priority band.

    Admins (the operators who own the deployment) get the reserved high band;
    every other authenticated user gets standard. Admin membership comes from
    ``auth.identity`` — the same predicate the entitlement API uses — rather than
    ``evaluate_admin``, on purpose: ``evaluate_admin`` additionally demands the
    ``X-Admin-Secret`` second factor, which is correct for *mutating* the
    entitlement API but would mean an admin browsing the app normally silently
    lost their band.

    Under spoofable auth (dev provider in a deployed environment) identity cannot
    be trusted, so nobody is promoted — the same fail-closed stance
    ``ai4ia_api.auth.admin`` takes.
    """
    if settings.auth_provider_is_spoofable:
        return PRIORITY_STANDARD
    return PRIORITY_HIGH if identity_is_admin(user, settings) else PRIORITY_STANDARD


def set_request_priority(value: int | None) -> None:
    _request_priority.set(value)


def get_request_priority() -> int | None:
    """The band for the current request, or None when unresolved/disabled."""
    return _request_priority.get()
