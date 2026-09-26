"""The capability probe and the one availability predicate for photo avatars.

Every seam that advertises or runs creation asks :func:`evaluate_availability`:
``GET /api/photo-avatars/config``, each avatar's ``usable`` flag, and the create
path immediately before it reserves anything. The order is fixed: flag, then
storage, then data residency, then application policy, then the Limited Access
capability.

The capability comes from the account's custom avatar features read through
the governed route. It is cached briefly and fails closed: an error, a timeout,
an unexpected shape or the feature's absence all mean unavailable. A create
that happens to succeed is never treated as entitlement. Status reads,
listing, previews, deletion and reports never depend on the capability, so an
avatar stays visible and deletable after access changes; it just stops being
``usable``.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ..policy.context import current_binding
from ..policy.dispatch import avatar_creation_zone_scoped
from ..policy.models import PolicyError, PolicyRequest
from .models import AvailabilityReason
from .provider import PhotoAvatarGateway

CapabilityState = Literal["entitled", "not_entitled", "unknown"]
PolicyState = Literal["allowed", "denied", "unavailable"]

CAPABILITY_TTL_SECONDS = 60.0
CAPABILITY_ERROR_TTL_SECONDS = 15.0


class CapabilityProbe:
    """Cached, single-flight read of the account's custom avatar features."""

    def __init__(
        self,
        gateway: PhotoAvatarGateway,
        required_feature: str,
        *,
        ttl: float = CAPABILITY_TTL_SECONDS,
        error_ttl: float = CAPABILITY_ERROR_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._required = required_feature
        self._ttl = ttl
        self._error_ttl = error_ttl
        self._clock = clock
        self._state: CapabilityState | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._state = None
        self._expires = 0.0

    async def status(self) -> CapabilityState:
        if self._state is not None and self._clock() < self._expires:
            return self._state
        async with self._lock:
            if self._state is not None and self._clock() < self._expires:
                return self._state
            features = await self._gateway.features()
            if features is None:
                state: CapabilityState = "unknown"
            else:
                # Exact ordinal membership: a similar name is not the approval.
                state = "entitled" if self._required in features else "not_entitled"
            self._state = state
            self._expires = self._clock() + (self._error_ttl if state == "unknown" else self._ttl)
            return state


@dataclass(frozen=True)
class Availability:
    reason: AvailabilityReason

    @property
    def available(self) -> bool:
        return self.reason == "available"


async def policy_state(operation: Literal["avatar.create", "avatar.use"]) -> PolicyState:
    """A pure snapshot of the current application policy, for advertising only.

    Execution requires the same operation through ``require_policy`` (the route
    inventory and the dispatch seam), so this never grants anything.
    """
    binding = current_binding()
    if binding is None:
        return "allowed"
    try:
        binding.require_configuration()
        effective = await binding.resolve()
        decision = binding.service.decide(effective, PolicyRequest(operation))
    except PolicyError as exc:
        return "unavailable" if exc.decision.outcome == "unavailable" else "denied"
    if not decision.allowed:
        return "unavailable" if decision.outcome == "unavailable" else "denied"
    if (
        operation == "avatar.create"
        and binding.service.enabled
        and avatar_creation_zone_scoped(effective)
    ):
        # The dispatch seam refuses this actor by the same rule.
        return "unavailable"
    return "allowed"


async def evaluate_availability(
    *,
    enabled: bool,
    storage_ready: bool,
    residency_ok: bool,
    policy: Callable[[], Awaitable[PolicyState]],
    capability: Callable[[], Awaitable[CapabilityState]],
) -> Availability:
    """Classify creation availability; only ``available`` may create.

    ``policy`` and ``capability`` are awaited lazily, in order, so a disabled
    or misconfigured deployment never probes the provider.
    """
    if not enabled:
        return Availability("disabled")
    if not storage_ready:
        return Availability("storage_unavailable")
    if not residency_ok:
        return Availability("residency_unsupported")
    state = await policy()
    if state == "denied":
        return Availability("policy_denied")
    if state != "allowed":
        return Availability("policy_unavailable")
    entitlement = await capability()
    if entitlement == "entitled":
        return Availability("available")
    if entitlement == "not_entitled":
        return Availability("capability_unavailable")
    return Availability("capability_unknown")
