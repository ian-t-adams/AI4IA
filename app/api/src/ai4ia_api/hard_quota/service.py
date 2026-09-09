"""Owner-scoped rolling reservations with one-shot dispatch and settlement."""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..entitlements.models import DAY_SECONDS, MINUTE_SECONDS, MONTH_SECONDS, EntitlementLimits
from .models import (
    MAX_ENTRIES,
    REPLAY_SECONDS,
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


class ReservationService:
    def __init__(self, store: ReservationStore) -> None:
        self.store = store

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

    @staticmethod
    def _reconcile(state: QuotaState, now: int) -> QuotaState:
        floor = max(state.replayFloor, now - REPLAY_SECONDS)
        entries: dict[str, Reservation] = {}
        for key, record in state.entries.items():
            if record.phase == "reserved" and record.expiresAt < now:
                record = record.model_copy(update={
                    "phase": "released", "settledAt": now, "charged": Amounts.zero(),
                })
            _, issued = parse_operation_id(key)
            # A record must have expired from BOTH replay retention and the
            # longest meter window. Unknown/dispatched work is never pruned.
            if (
                not record.protected and issued < floor
                and record.settledAt is not None and record.settledAt < now - MONTH_SECONDS
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

    @staticmethod
    def _check_limits(
        state: QuotaState, amounts: Amounts, limits: EntitlementLimits, now: int,
        *, excluding: str | None = None,
    ) -> None:
        for limit_name, dimension, seconds in _WINDOWS:
            cap = getattr(limits, limit_name)
            if cap is None or (dimension == "compute" and amounts.compute == 0):
                continue
            requested = getattr(amounts, dimension)
            if requested is None:
                raise QuotaError(f"Hard quota does not support this operation under {limit_name}.")
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
            complete = (
                outcome == "complete" and actual is not None
                and all(
                    getattr(record.bounds.amounts, dimension) is None
                    or getattr(actual, dimension) is not None
                    for dimension in ("tokens", "microUsd")
                )
            )
            charged = actual if complete else record.bounds.amounts
            assert charged is not None
            # Requests/compute count dispatch, not successful output. Never
            # let a provider-usage value refund either attempt counter.
            charged = charged.model_copy(update={
                "requests": record.bounds.amounts.requests,
                "compute": record.bounds.amounts.compute,
            })
            exceeded = any(
                getattr(charged, dimension) is not None
                and getattr(record.bounds.amounts, dimension) is not None
                and getattr(charged, dimension) > getattr(record.bounds.amounts, dimension)
                for dimension in ("tokens", "microUsd")
            )
            record = record.model_copy(update={
                "phase": "settled" if complete else "unknown", "outcome": outcome,
                "settledAt": now, "settlementDigest": settlement, "charged": charged,
            })
            return state.model_copy(update={
                "entries": {**state.entries, record.operationId: record},
                "blocked": state.blocked or exceeded,
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
