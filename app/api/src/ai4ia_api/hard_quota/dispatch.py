"""Authenticated owner propagation and the common pre-egress admission seam."""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import httpx

from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..usage.pricing import PricingBook
from .coverage import AttemptEnvelope, actual_amounts, reservation_bounds
from .models import (
    MAX_ADMISSION_EVIDENCE, AdmissionEvidence, Outcome, QuotaError, Reservation, Surface, operation_id,
)
from .service import ReservationService
from .store import ReservationStore

if TYPE_CHECKING:
    from ..policy.service import PolicyService

@dataclass
class AdmissionContext:
    controller: AdmissionController
    owner: str
    root: str = field(default_factory=lambda: uuid.uuid4().hex)
    issued_at: int | None = None
    sequence: int = 0
    evidence: list[AdmissionEvidence] = field(default_factory=list)
    evidence_count: int = 0


_current: ContextVar[AdmissionContext | None] = ContextVar("hard_quota_owner", default=None)


def clear_admission_owner() -> None:
    _current.set(None)


def set_admission_owner(controller: AdmissionController, owner: str) -> None:
    """Only authentication boundaries or a validated durable owner may call."""
    _current.set(AdmissionContext(controller, owner))


@contextmanager
def admission_scope(
    controller: AdmissionController, owner: str, *, root: str | None = None,
    issued_at: int | None = None,
) -> Iterator[AdmissionContext]:
    context = AdmissionContext(controller, owner, root or uuid.uuid4().hex, issued_at)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def current_admission_evidence(owner: str) -> tuple[tuple[AdmissionEvidence, ...], int | None]:
    context = _current.get()
    if context is None or context.owner != owner or not context.controller.enabled:
        return (), None
    return tuple(context.evidence), context.evidence_count


@dataclass
class DispatchLease:
    reservation: Reservation | None = None
    outcome: Outcome = "error"
    usage: dict[str, Any] | None = None
    completed: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def report(self, usage: dict[str, Any] | None = None, *, complete: bool = True) -> None:
        if usage is not None:
            self.usage = usage
        self.completed = self.completed or complete
        if complete:
            self.outcome = "complete"


class AdmissionController:
    def __init__(
        self, *, entitlements: EntitlementService, catalog: ModelCatalog,
        pricing: PricingBook, store: ReservationStore | None = None, enabled: bool = False,
        attempts: AttemptEnvelope | None = None,
        policy: PolicyService | None = None,
    ) -> None:
        self.enabled = enabled
        self.entitlements = entitlements
        self.catalog = catalog
        self.pricing = pricing
        self.attempts = attempts
        self.policy = policy
        self.reservations = ReservationService(store) if store is not None else None

    async def claim(
        self, context: AdmissionContext, surface: Surface, payload: dict[str, Any],
        deployment: str | None, target: str | None,
    ) -> Reservation | None:
        policy = (
            await self.entitlements.get_for_admission(context.owner)
            if self.enabled else await self.entitlements.get_effective(context.owner)
        )
        if policy.disabled:
            raise QuotaError("This account is disabled.", code=403)
        if not self.enabled:
            return None
        if self.reservations is None:
            raise QuotaError("Hard quota coordination is unavailable.")
        snapshot = await self.reservations.store.read(context.owner)
        if context.issued_at is None:
            context.issued_at = snapshot.now
        # Increment before the next await: sibling tasks never share an identity.
        context.sequence += 1
        key = operation_id(snapshot.state.epoch, context.issued_at, f"{context.root}:{context.sequence}")
        bounds = reservation_bounds(
            surface, payload, deployment=deployment, catalog=self.catalog, pricing=self.pricing,
            attempts=self.attempts,
        )
        reservation = await self.reservations.reserve(
            context.owner, key=key,
            payload={"deployment": deployment, "target": target, "body": payload},
            surface=surface, bounds=bounds, limits=policy,
        )
        # Re-read policy after coordination and immediately before claiming egress.
        # A newly disabled owner may never use a reservation as an authorization.
        latest = await self.entitlements.get_for_admission(context.owner)
        if latest.disabled:
            await self.reservations.release(context.owner, reservation)
            raise QuotaError("This account is disabled.", code=403)
        if latest.model_dump() != policy.model_dump():
            await self.reservations.release(context.owner, reservation)
            raise QuotaError("Hard quota policy changed before dispatch.", code=409)
        return await self.reservations.dispatch(context.owner, reservation)


@asynccontextmanager
async def admitted_dispatch(
    surface: Surface, payload: dict[str, Any], *, deployment: str | None = None,
    target: str | None = None,
    required: bool = False, observe: Callable[[AdmissionEvidence], None] | None = None,
    policy_required: bool = False,
) -> AsyncIterator[DispatchLease]:
    from ..policy.dispatch import authorize_dispatch

    context = _current.get()
    await authorize_dispatch(
        surface, deployment=deployment, required=policy_required,
        expected_owner=context.owner if context is not None else None,
        service=context.controller.policy if context is not None else None,
    )
    if context is None:
        if required:
            raise QuotaError("Hard quota dispatch has no authenticated owner.")
        yield DispatchLease(payload=payload)
        return
    if required and not context.controller.enabled:
        raise QuotaError("Hard quota dispatch has incompatible coordination.")
    # Snapshot before the first policy/store await. The transport sends this
    # exact snapshot, so concurrent caller mutations cannot change an admitted
    # tool argument or model request behind its immutable digest/bound.
    frozen = (
        json.loads(json.dumps(payload, ensure_ascii=True, allow_nan=False))
        if context.controller.enabled else payload
    )
    record = await context.controller.claim(context, surface, frozen, deployment, target)
    lease = DispatchLease(reservation=record, payload=frozen)
    evidence_index = None
    if record is not None:
        context.evidence_count += 1
        pending = AdmissionEvidence.from_record(record)
        if len(context.evidence) < MAX_ADMISSION_EVIDENCE:
            evidence_index = len(context.evidence)
            context.evidence.append(pending)
        if observe is not None:
            observe(pending)
    try:
        yield lease
    except asyncio.CancelledError:
        lease.outcome = "cancelled"
        lease.completed = False
        raise
    except (TimeoutError, httpx.TimeoutException):
        lease.outcome = "timeout"
        lease.completed = False
        raise
    finally:
        service = context.controller.reservations
        if record is not None and service is not None:
            actual = actual_amounts(record.bounds, lease.usage) if lease.completed else None
            settled = await service.settle(
                context.owner, record, outcome=lease.outcome, actual=actual,
            )
            evidence = AdmissionEvidence.from_record(settled)
            if evidence_index is not None:
                context.evidence[evidence_index] = evidence
            if observe is not None:
                observe(evidence)
