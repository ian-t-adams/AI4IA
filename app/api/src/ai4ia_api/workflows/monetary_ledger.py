"""Monetary mutations committed with the existing owner/effect ETag CAS."""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..hard_quota.coverage import actual_amounts
from ..hard_quota.models import MAX_QUANTITY, Bounds
from .automation_common import AutomationError, digest, json_bytes
from .monetary_models import DispatchMoney, RunMoney

if TYPE_CHECKING:
    from .automation_models import AutomationOwner, EffectIntent


def _updated(account: RunMoney, **changes: Any) -> RunMoney:
    return RunMoney.model_validate({
        **account.model_dump(), **changes, "revision": account.revision + 1,
    })


def reserve_money(
    owner: AutomationOwner, run_id: str, bounds: Bounds, *,
    approval_spend_digest: str | None = None,
) -> DispatchMoney:
    handle = owner.runs[run_id]
    account = handle.money
    if account is None:
        raise AutomationError("budget_missing", "The capped run has no monetary accounting contract.")
    amount = bounds.amounts.microUsd
    if amount is None or bounds.maxAttempts != 1:
        raise AutomationError("spend_unbounded", "This operation has no proven USD application-meter bound.")
    if handle.terminal or not handle.active or account.blocked:
        raise AutomationError("budget_stopped", "This run no longer admits monetary reservations.")
    if amount > account.remaining_micro_usd:
        raise AutomationError("spend_limit", "The operation exceeds the run's remaining USD application-meter budget.")
    reservation = DispatchMoney(
        budgetId=account.budgetId, budgetRevision=account.revision,
        bounds=bounds, phase="held", chargedMicroUsd=amount, settlementDigest=None,
        approvalSpendDigest=approval_spend_digest,
    )
    handle.money = _updated(
        account, heldMicroUsd=account.heldMicroUsd + amount,
        reservations=account.reservations + 1,
    )
    return reservation


def settle_money(
    owner: AutomationOwner, effect: EffectIntent, *, completed: bool,
    usage: dict[str, Any] | None, outcome: str,
) -> None:
    reservation = effect.money
    account = owner.runs[effect.runId].money
    if reservation is None or account is None or reservation.budgetId != account.budgetId:
        raise AutomationError("budget_missing", "The dispatch lost its immutable monetary reservation.")
    identity = digest({"completed": completed, "usage": usage, "outcome": outcome})
    if reservation.phase != "held":
        if reservation.settlementDigest == identity:
            return
        raise AutomationError("accounting_changed", "The monetary outcome is already recorded differently.")
    actual = actual_amounts(reservation.bounds, usage) if completed else None
    charged = actual.microUsd if actual is not None else None
    reason = account.reason
    if charged is not None and account.settledMicroUsd + charged > MAX_QUANTITY:
        charged = None
        reason = "accounting_overflow"
    if actual is not None and (
        (actual.microUsd is not None and actual.microUsd > reservation.chargedMicroUsd)
        or (
            actual.tokens is not None and reservation.bounds.amounts.tokens is not None
            and actual.tokens > reservation.bounds.amounts.tokens
        )
    ):
        reason = reason or "bound_exceeded"
    if charged is None:
        updated = reservation.model_copy(update={"phase": "unknown", "settlementDigest": identity})
        revised = _updated(
            account, unknownMicroUsd=account.unknownMicroUsd + reservation.chargedMicroUsd,
            blocked=reason is not None, reason=reason,
        )
    else:
        updated = reservation.model_copy(update={
            "phase": "settled", "chargedMicroUsd": charged, "settlementDigest": identity,
        })
        revised = _updated(
            account, settledMicroUsd=account.settledMicroUsd + charged,
            heldMicroUsd=account.heldMicroUsd - reservation.chargedMicroUsd,
            blocked=reason is not None, reason=reason,
        )
    effect.money = DispatchMoney.model_validate(updated.model_dump())
    owner.runs[effect.runId].money = revised


def compact_money(owner: AutomationOwner, effect: EffectIntent) -> None:
    reservation = effect.money
    if reservation is None:
        return
    account = owner.runs[effect.runId].money
    if account is None or reservation.phase != "settled":
        raise AutomationError("accounting_changed", "An unresolved monetary liability cannot be pruned.")
    owner.runs[effect.runId].money = _updated(
        account, compactedMicroUsd=account.compactedMicroUsd + reservation.chargedMicroUsd,
        compactedReservations=account.compactedReservations + 1,
    )


def validate_money_balances(owner: AutomationOwner) -> None:
    for handle in owner.runs.values():
        account = handle.money
        dispatches = [
            effect for effect in owner.effects.values()
            if effect.runId == handle.runId and effect.category == "dispatch"
        ]
        if account is None:
            if any(effect.money is not None for effect in dispatches):
                raise ValueError("a monetary dispatch has no matching run account")
            continue
        settled, held, unknown = account.compactedMicroUsd, 0, 0
        for effect in dispatches:
            money = effect.money
            if money is None or money.budgetId != account.budgetId:
                raise ValueError("a capped dispatch lost its monetary contract")
            if money.phase == "settled":
                if effect.state != "complete":
                    raise ValueError("settled money has no completed effect")
                settled += money.chargedMicroUsd
            else:
                if effect.state not in {"reserved", "dispatched", "unknown"}:
                    raise ValueError("an unresolved monetary liability became complete")
                held += money.chargedMicroUsd
                if money.phase == "unknown":
                    unknown += money.chargedMicroUsd
        if (
            account.settledMicroUsd != settled or account.heldMicroUsd != held
            or account.unknownMicroUsd != unknown
            or account.reservations != account.compactedReservations + len(dispatches)
        ):
            raise ValueError("persisted monetary totals lost a dispatch or compacted charge")


def validate_money_transition(prior: AutomationOwner, updated: AutomationOwner) -> None:
    for run_id, old in prior.runs.items():
        new = updated.runs.get(run_id)
        if old.money is None:
            if new is not None and new.money is not None:
                raise AutomationError("budget_changed", "An existing run cannot acquire a new monetary budget.")
            continue
        if new is None:
            if (
                not old.terminal or old.active
                or old.money.heldMicroUsd or old.createdAt >= updated.requestFloor
            ):
                raise AutomationError("accounting_changed", "Unresolved run money cannot be retired.")
            continue
        before, after = old.money, new.money
        if (
            after is None or before.budgetId != after.budgetId
            or before.limitMicroUsd != after.limitMicroUsd
            or after.revision < before.revision
            or after.settledMicroUsd < before.settledMicroUsd
            or after.compactedMicroUsd < before.compactedMicroUsd
            or after.compactedReservations < before.compactedReservations
            or after.reservations < before.reservations
            or (old.terminal and not new.terminal)
            or (not old.active and new.active)
            or (before.blocked and (not after.blocked or after.reason != before.reason))
            or (before != after and after.revision == before.revision)
        ):
            raise AutomationError("budget_changed", "The immutable run budget or prior accounting changed.")
    for identity, old in prior.effects.items():
        if old.money is None:
            continue
        new = updated.effects.get(identity)
        if new is None:
            handle = updated.runs.get(old.runId)
            if (
                old.money.phase != "settled" or old.state != "complete"
                or (old.usage is not None and not old.delivered)
                or (
                    handle is not None
                    and handle.operationFloor <= prior.runs[old.runId].operationFloor
                )
            ):
                raise AutomationError("accounting_changed", "A monetary effect cannot lose its replay fence.")
            continue
        money = new.money
        if (
            money is None or new.payloadDigest != old.payloadDigest
            or new.runId != old.runId or new.operationId != old.operationId
            or money.budgetId != old.money.budgetId or money.bounds != old.money.bounds
            or money.budgetRevision != old.money.budgetRevision
            or money.approvalSpendDigest != old.money.approvalSpendDigest
            or (old.money.phase != "held" and money != old.money)
        ):
            raise AutomationError("accounting_changed", "The reserved payload, price or outcome cannot change.")


def monetary_transition_bytes(body: dict[str, Any]) -> int:
    """Additional escaped space for every outstanding monetary transition."""
    future = {**body, "runs": {}, "effects": {}}
    for identity, run in body["runs"].items():
        changed = dict(run)
        money = run.get("money")
        if money is not None:
            projected = dict(money)
            for name in (
                "revision", "settledMicroUsd", "heldMicroUsd", "unknownMicroUsd", "compactedMicroUsd",
            ):
                projected[name] = MAX_QUANTITY
            projected.update(
                compactedReservations=128, reservations=128, blocked=False, reason="accounting_overflow",
            )
            changed["money"] = projected
        future["runs"][identity] = changed
    for identity, effect in body["effects"].items():
        changed = dict(effect)
        money = effect.get("money")
        if money is not None and money["phase"] == "held":
            changed["money"] = {
                **money, "phase": "unknown", "chargedMicroUsd": MAX_QUANTITY,
                "settlementDigest": "f" * 64,
            }
        future["effects"][identity] = changed
    # Include the SDK's ordinary JSON separators as well as ASCII escaping;
    # the canonical digest encoding deliberately omits those spaces.
    return max(
        0, len(json.dumps(future, ensure_ascii=True, allow_nan=False).encode("ascii")) - len(json_bytes(body)),
    )
