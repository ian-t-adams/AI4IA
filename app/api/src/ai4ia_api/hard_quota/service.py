"""Owner-scoped rolling reservations with one-shot dispatch and settlement."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..entitlements.models import DAY_SECONDS, MINUTE_SECONDS, MONTH_SECONDS, EntitlementLimits
from .models import (
    MAX_ENTRIES,
    MAX_QUANTITY,
    REPLAY_SECONDS,
    REQUEST_COUNT_REPLAY_SECONDS,
    RESERVATION_SECONDS,
    Amounts,
    Bounds,
    Outcome,
    QuotaError,
    QuotaState,
    Reservation,
    Snapshot,
    Surface,
    canonical_digest,
    ensure_transition_capacity,
    parse_operation_id,
    state_document,
)
from .store import ReservationStore

T = TypeVar("T")
MAX_CAS_ATTEMPTS = 16
_WINDOWS = (
    ("requestsPerMinute", "requests", MINUTE_SECONDS),
    ("tokensPerDay", "tokens", DAY_SECONDS),
    ("tokensPerMonth", "tokens", MONTH_SECONDS),
    ("costPerDayMicroUsd", "microUsd", DAY_SECONDS),
    ("costPerMonthMicroUsd", "microUsd", MONTH_SECONDS),
    ("computeExecutionsPerDay", "compute", DAY_SECONDS),
)


@dataclass(frozen=True)
class RequestCountScope:
    """Contract of an approved request-count rollout, never a configuration flag.

    Only the production factory constructs it, from a validated operator record.
    ``coverage_start`` is the store-clock instant after which every writer was
    proven to enforce admission; dispatch history before it is unknown.
    """

    coverage_start: int

    def __post_init__(self) -> None:
        if type(self.coverage_start) is not int or not 0 <= self.coverage_start <= MAX_QUANTITY:
            raise ValueError("Invalid hard quota rollout coverage.")


class ReservationService:
    def __init__(self, store: ReservationStore, *, scope: RequestCountScope | None = None) -> None:
        self.store = store
        # None is the historical contract used by tests and the local fake.
        self.scope = scope

    async def _change(
        self, owner: str, change: Callable[[QuotaState, int], tuple[QuotaState, T]]
    ) -> T:
        for _attempt in range(MAX_CAS_ATTEMPTS):
            snapshot = await self.store.read(owner)
            self._validate_snapshot(owner, snapshot)
            now = max(snapshot.now, snapshot.state.observedAt)
            state = self._reconcile(snapshot.state, now).model_copy(update={"observedAt": now})
            updated, result = change(state, now)
            updated = updated.model_copy(update={"observedAt": now})
            if await self.store.replace(owner, snapshot, updated):
                return result
        raise QuotaError("Hard quota coordination is busy; no dispatch was admitted.")

    @staticmethod
    def _validate_snapshot(owner: str, snapshot: Snapshot) -> None:
        state_document(snapshot.state)
        if snapshot.state.userId != owner:
            raise QuotaError("Hard quota owner mismatch.", code=403)
        if not snapshot.etag or snapshot.now < snapshot.state.observedAt:
            raise QuotaError("Hard quota coordination state is incompatible.")

    def _retention(self, record: Reservation) -> int:
        if self.scope is None:
            return MONTH_SECONDS
        # Only request and compute windows can admit in this scope: a compute
        # attempt counts for 24h, every other dispatch only in the 60s window.
        if record.surface == "compute" and record.phase == "settled":
            return DAY_SECONDS
        return MINUTE_SECONDS

    def _reconcile(self, state: QuotaState, now: int) -> QuotaState:
        replay = REPLAY_SECONDS if self.scope is None else REQUEST_COUNT_REPLAY_SECONDS
        floor = max(state.replayFloor, now - replay)
        entries: dict[str, Reservation] = {}
        for key, record in state.entries.items():
            if record.phase == "reserved" and record.expiresAt < now:
                record = record.model_copy(update={
                    "phase": "released", "settledAt": now, "charged": Amounts.zero(),
                })
            _, issued = parse_operation_id(key)
            # A record must have expired from BOTH replay retention and the
            # longest window that can count it. Unknown/dispatched work is never pruned.
            if (
                not record.protected and issued < floor
                and record.settledAt is not None
                and record.settledAt < now - self._retention(record)
            ):
                continue
            entries[key] = record
        return state.model_copy(update={"entries": entries, "replayFloor": floor})

    @staticmethod
    def _identity(state: QuotaState, key: str, now: int) -> None:
        epoch, issued = parse_operation_id(key)
        if epoch != state.epoch or issued < state.replayFloor or issued > now:
            raise QuotaError("Hard quota operation identity is expired or incompatible.", code=409)

    async def reserve(
        self, owner: str, *, key: str, payload: object, surface: Surface,
        bounds: Bounds, limits: EntitlementLimits,
    ) -> Reservation:
        digest = canonical_digest({
            "owner": owner, "surface": surface, "payload": payload,
            "bounds": bounds.model_dump(mode="json"),
        })

        def change(state: QuotaState, now: int) -> tuple[QuotaState, Reservation]:
            self._identity(state, key, now)
            if bounds.amounts.requests != 1:
                raise QuotaError("A hard quota reservation must cover one dispatch.")
            if bounds.amounts.compute != (1 if surface == "compute" else 0):
                raise QuotaError("Hard quota compute units do not match the dispatch surface.")
            if limits.disabled:
                raise QuotaError("This account is disabled.", code=403)
            if self.scope is not None and (
                bounds.amounts.tokens is not None or bounds.amounts.microUsd is not None
            ):
                raise QuotaError(
                    "Hard quota token or dollar bounds are outside the approved request-count rollout.",
                )
            if state.blocked:
                raise QuotaError("Hard quota state requires reviewed reconciliation.")
            prior = state.entries.get(key)
            if prior is not None:
                if prior.payloadDigest != digest:
                    raise QuotaError("Hard quota operation payload changed.", code=409)
                if prior.phase == "reserved":
                    self._check_limits(
                        state, prior.bounds.amounts, limits, now, excluding=key,
                    )
                    ensure_transition_capacity(state)
                return state, prior
            self._check_limits(state, bounds.amounts, limits, now)
            if len(state.entries) >= MAX_ENTRIES:
                raise QuotaError("Hard quota coordination capacity is exhausted.")
            record = Reservation(
                operationId=key, payloadDigest=digest, surface=surface, bounds=bounds,
                reservedAt=now, expiresAt=now + RESERVATION_SECONDS, charged=bounds.amounts,
            )
            updated = state.model_copy(update={"entries": {**state.entries, key: record}})
            ensure_transition_capacity(updated)
            return updated, record

        return await self._change(owner, change)

    def _check_limits(
        self, state: QuotaState, amounts: Amounts, limits: EntitlementLimits, now: int,
        *, excluding: str | None = None,
    ) -> None:
        # Seeding fence: this document is authoritative only from its creation
        # and from the approved all-writer cutover, whichever is later.
        fence = None
        if self.scope is not None:
            fence = max(state.validAfter, self.scope.coverage_start)
            for limit_name, dimension, _seconds in _WINDOWS:
                # Refused before any history is read: this rollout never
                # evaluates, retains or claims token/dollar windows.
                if dimension in {"tokens", "microUsd"} and getattr(limits, limit_name) is not None:
                    raise QuotaError(
                        f"Hard quota {limit_name} is outside the approved request-count rollout.",
                    )
        for limit_name, dimension, seconds in _WINDOWS:
            cap = getattr(limits, limit_name)
            if cap is None or (dimension == "compute" and amounts.compute == 0):
                continue
            requested = getattr(amounts, dimension)
            if requested is None:
                raise QuotaError(f"Hard quota does not support this operation under {limit_name}.")
            if fence is not None and now - seconds < fence:
                # Unrecorded pre-cutover dispatches may fill the uncovered part
                # of this window, so it counts as already consumed.
                raise QuotaError(
                    f"Hard quota {limit_name} history before cutover counts as consumed.",
                    code=429,
                )
            used = 0
            for record in state.entries.values():
                if record.operationId == excluding:
                    continue
                if not record.protected and (record.settledAt or 0) < now - seconds:
                    continue
                charge = getattr(record.charged, dimension)
                if charge is None:
                    raise QuotaError(f"Hard quota has unknown prior usage under {limit_name}.")
                used += charge
            if used + requested > cap:
                raise QuotaError(f"Hard quota {limit_name} would be exceeded.", code=429)

    async def dispatch(self, owner: str, reservation: Reservation) -> Reservation:
        def change(state: QuotaState, now: int) -> tuple[QuotaState, Reservation]:
            record = self._owned_record(state, reservation)
            # Only the winner of this CAS may invoke the provider. A retry that
            # finds 'dispatched' is ambiguous and must NOT resend the request.
            if record.phase != "reserved":
                raise QuotaError("Hard quota operation was already claimed or expired.", code=409)
            if state.blocked:
                raise QuotaError("Hard quota state requires reviewed reconciliation.")
            ensure_transition_capacity(state)
            record = record.model_copy(update={"phase": "dispatched", "dispatchedAt": now})
            return state.model_copy(update={"entries": {
                **state.entries, record.operationId: record,
            }}), record

        return await self._change(owner, change)

    @staticmethod
    def _owned_record(state: QuotaState, reservation: Reservation) -> Reservation:
        record = state.entries.get(reservation.operationId)
        if record is None or record.payloadDigest != reservation.payloadDigest:
            raise QuotaError("Hard quota operation does not belong to this owner.", code=403)
        return record

    async def settle(
        self, owner: str, reservation: Reservation, *, outcome: Outcome,
        actual: Amounts | None = None,
    ) -> Reservation:
        settlement = canonical_digest({
            "outcome": outcome, "actual": actual.model_dump(mode="json") if actual else None,
        })

        def change(state: QuotaState, now: int) -> tuple[QuotaState, Reservation]:
            record = self._owned_record(state, reservation)
            if record.settlementDigest is not None:
                if record.settlementDigest != settlement:
                    raise QuotaError("Hard quota settlement changed.", code=409)
                return state, record
            if record.phase != "dispatched":
                raise QuotaError("Hard quota operation was not dispatched.", code=409)
            complete = record.has_complete_usage(outcome, actual)
            # In the approved request-count scope a request-only operation's
            # enforced quantities are its attempt counts, already fixed at
            # dispatch; any terminal outcome settles them as known history.
            attempt_only = self.scope is not None and record.request_only
            charged = actual if complete else record.bounds.amounts
            assert charged is not None
            # Requests/compute count dispatch, not successful output. Never
            # let a provider-usage value refund either attempt counter.
            charged = charged.model_copy(update={
                "requests": record.bounds.amounts.requests,
                "compute": record.bounds.amounts.compute,
            })
            record = record.model_copy(update={
                "phase": "settled" if complete or attempt_only else "unknown", "outcome": outcome,
                "settledAt": now, "settlementDigest": settlement, "charged": charged,
            })
            return state.model_copy(update={
                "entries": {**state.entries, record.operationId: record},
                "blocked": state.blocked or record.exceeds_bound,
            }), record

        return await self._change(owner, change)

    async def release(self, owner: str, reservation: Reservation) -> Reservation:
        def change(state: QuotaState, now: int) -> tuple[QuotaState, Reservation]:
            record = self._owned_record(state, reservation)
            if record.phase == "released":
                return state, record
            if record.phase != "reserved":
                raise QuotaError("Dispatched hard quota work cannot be released.", code=409)
            record = record.model_copy(update={
                "phase": "released", "settledAt": now, "charged": Amounts.zero(),
            })
            return state.model_copy(update={"entries": {
                **state.entries, record.operationId: record,
            }}), record

        return await self._change(owner, change)

    async def reconcile(self, owner: str) -> QuotaState:
        return await self._change(owner, lambda state, _now: (state, state))
