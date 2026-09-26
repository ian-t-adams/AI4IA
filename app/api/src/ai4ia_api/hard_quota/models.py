"""Bounded, credential-free rolling-admission persistence contract."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from ..entitlements.models import MONTH_SECONDS

STATE_ID = "hard-quota-state-v1"
STATE_KIND = "hard_quota_state"
POLICY_VERSION = "rolling-dispatch-v1"
# Operator-authored activation records share the existing usage container in a
# partition no internal (UUID) owner id can equal. Nothing in the app writes it.
CONTROL_PARTITION = "__ai4ia_hard_quota_control__"
ROLLOUT_KIND = "hard_quota_rollout_v1"
MAX_ENTRIES = 1024
MAX_STATE_BYTES = 512 * 1024
REPLAY_SECONDS = MONTH_SECONDS
# The approved request-count scope never evaluates token/dollar windows, so its
# identities only need to outlive one claim's store round trips.
REQUEST_COUNT_REPLAY_SECONDS = 300
RESERVATION_SECONDS = 120
MAX_ADMISSION_EVIDENCE = 8
MAX_QUANTITY = 2**53 - 1

Count = Annotated[int, Field(strict=True, ge=0, le=MAX_QUANTITY)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Epoch = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Owner = Annotated[str, Field(min_length=1, max_length=128)]
Surface = Literal[
    "chat", "embedding", "image", "video", "transcription", "speech",
    "realtime", "compute", "document", "web_search", "mcp", "external_tool", "avatar",
]
Outcome = Literal["complete", "cancelled", "error", "timeout", "unknown"]
Phase = Literal["reserved", "dispatched", "settled", "unknown", "released"]


class QuotaError(RuntimeError):
    """Safe public reason; never include a payload, owner, or provider error."""

    def __init__(self, reason: str, *, code: int = 503) -> None:
        super().__init__(reason)
        self.code = code


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def require_persisted_fields(cls, value: Any, info: ValidationInfo) -> Any:
        # Context propagates through nested Pydantic models. Construction may
        # use defaults; a persisted v1 record must explicitly contain every key,
        # including null unsupported axes and null not-yet-reached timestamps.
        if info.context and info.context.get("persisted_quota"):
            if not isinstance(value, dict) or cls.model_fields.keys() - value.keys():
                raise ValueError("incomplete persisted quota accounting")
        return value


class Amounts(ContractModel):
    requests: Count = 1
    # None means unsupported/unknown, NOT free. It blocks that capped dimension.
    tokens: Count | None = None
    microUsd: Count | None = None
    compute: Count = 0

    @classmethod
    def zero(cls) -> Amounts:
        return cls(requests=0, tokens=0, microUsd=0, compute=0)


class Bounds(ContractModel):
    amounts: Amounts
    basis: Literal["request-v1", "catalog-text-v1", "catalog-embedding-v1", "compute-v1"]
    priceVersion: Annotated[str, Field(max_length=96)] | None = None
    inputRate: Annotated[str, Field(max_length=40)] | None = None
    outputRate: Annotated[str, Field(max_length=40)] | None = None
    attemptVersion: Annotated[str, Field(max_length=96)] | None = None
    maxAttempts: Count | None = None

    @model_validator(mode="after")
    def dollar_bound_requires_versioned_rates(self) -> Bounds:
        if self.amounts.microUsd is not None and (
            not self.priceVersion or self.inputRate is None or self.outputRate is None
            or not self.attemptVersion or self.maxAttempts is None or self.maxAttempts < 1
        ):
            raise ValueError("dollar reservation requires versioned conservative bounds")
        return self


class Reservation(ContractModel):
    operationId: Annotated[str, Field(max_length=120)]
    payloadDigest: Digest
    surface: Surface
    bounds: Bounds
    phase: Phase = "reserved"
    reservedAt: Count
    expiresAt: Count
    dispatchedAt: Count | None = None
    settledAt: Count | None = None
    outcome: Outcome | None = None
    settlementDigest: Digest | None = None
    charged: Amounts

    def has_complete_usage(self, outcome: Outcome | None, actual: Amounts | None) -> bool:
        return (
            outcome == "complete" and actual is not None
            and all(
                getattr(self.bounds.amounts, dimension) is None
                or getattr(actual, dimension) is not None
                for dimension in ("tokens", "microUsd")
            )
        )

    @property
    def request_only(self) -> bool:
        """No bounded token/dollar axis: only the attempt counters are metered."""
        return self.bounds.amounts.tokens is None and self.bounds.amounts.microUsd is None

    def is_attempt_settlement(self, outcome: Outcome | None, charged: Amounts) -> bool:
        # The one-shot dispatch claim fixes the request/compute attempt counts, and
        # every send of the operation precedes its terminal settlement. With no
        # bounded token/dollar axis nothing unknown remains, provided the charge is
        # exactly the frozen attempt bound. Only the approved request-count scope
        # writes this shape for non-complete outcomes.
        return self.request_only and outcome is not None and charged == self.bounds.amounts

    @property
    def exceeds_bound(self) -> bool:
        return self.phase == "settled" and any(
            bound is not None and charge is not None and charge > bound
            for bound, charge in (
                (self.bounds.amounts.tokens, self.charged.tokens),
                (self.bounds.amounts.microUsd, self.charged.microUsd),
            )
        )

    @property
    def protected(self) -> bool:
        # Unknown/ambiguous dispatches never age out or get refunded by a lease.
        return self.phase in {"reserved", "dispatched", "unknown"}


class AdmissionEvidence(ContractModel):
    policyVersion: Literal["rolling-dispatch-v1"] = POLICY_VERSION
    operationHash: Digest
    surface: Surface
    phase: Phase
    reserved: Amounts
    charged: Amounts
    priceVersion: Annotated[str, Field(max_length=96)] | None = None
    attemptVersion: Annotated[str, Field(max_length=96)] | None = None

    @classmethod
    def from_record(cls, record: Reservation) -> AdmissionEvidence:
        return cls(
            operationHash=hashlib.sha256(record.operationId.encode("utf-8")).hexdigest(),
            surface=record.surface, phase=record.phase,
            reserved=record.bounds.amounts, charged=record.charged,
            priceVersion=record.bounds.priceVersion, attemptVersion=record.bounds.attemptVersion,
        )


class QuotaState(ContractModel):
    id: Literal["hard-quota-state-v1"] = STATE_ID
    kind: Literal["hard_quota_state"] = STATE_KIND
    policyVersion: Literal["rolling-dispatch-v1"] = POLICY_VERSION
    userId: Owner
    epoch: Epoch
    # These are cutover/retention fences, not an acknowledgement of bootstrap.
    validAfter: Count
    replayFloor: Count
    observedAt: Count
    blocked: bool = Field(default=False, strict=True)
    entries: dict[str, Reservation] = Field(default_factory=dict, max_length=MAX_ENTRIES)

    @model_validator(mode="after")
    def validate_entries(self) -> QuotaState:
        if self.replayFloor < self.validAfter or self.observedAt < self.replayFloor:
            raise ValueError("invalid quota time fences")
        for key, entry in self.entries.items():
            compute = 1 if entry.surface == "compute" else 0
            if entry.bounds.amounts.requests != 1 or entry.bounds.amounts.compute != compute:
                raise ValueError("quota bounds do not match the dispatch surface")
            released = entry.phase == "released"
            if (
                entry.charged.requests != (0 if released else 1)
                or entry.charged.compute != (0 if released else compute)
            ):
                raise ValueError("quota charge lost its dispatch attempt")
            if key != entry.operationId:
                raise ValueError("invalid quota operation key")
            epoch, issued = parse_operation_id(key)
            if epoch != self.epoch or issued < self.validAfter:
                raise ValueError("invalid quota operation scope")
            if entry.expiresAt < entry.reservedAt:
                raise ValueError("invalid quota reservation lease")
            if entry.phase in {"reserved", "released"} and entry.dispatchedAt is not None:
                raise ValueError("undispatched operation has dispatch evidence")
            if entry.phase in {"dispatched", "settled", "unknown"} and entry.dispatchedAt is None:
                raise ValueError("missing quota dispatch evidence")
            if entry.phase in {"settled", "unknown", "released"} and entry.settledAt is None:
                raise ValueError("missing quota settlement evidence")
            if entry.phase in {"reserved", "dispatched"} and entry.settledAt is not None:
                raise ValueError("unfinished operation has settlement evidence")
            if entry.phase in {"settled", "unknown"}:
                if entry.outcome is None or entry.settlementDigest is None:
                    raise ValueError("missing quota settlement identity")
                if entry.phase == "settled" and not (
                    entry.has_complete_usage(entry.outcome, entry.charged)
                    or entry.is_attempt_settlement(entry.outcome, entry.charged)
                ):
                    raise ValueError("incomplete known quota settlement")
            elif entry.outcome is not None or entry.settlementDigest is not None:
                raise ValueError("unsettled operation has settlement identity")
            if entry.exceeds_bound and not self.blocked:
                raise ValueError("known quota bound violation requires blocked state")
            if entry.phase == "released" and entry.charged != Amounts.zero():
                raise ValueError("released operation retains a charge")
            if entry.protected and entry.charged != entry.bounds.amounts:
                raise ValueError("unsettled or unknown reservation lost its bound")
            if entry.reservedAt > self.observedAt:
                raise ValueError("quota reservation is ahead of the coordination clock")
            if entry.dispatchedAt is not None and (
                entry.dispatchedAt < entry.reservedAt or entry.dispatchedAt > entry.expiresAt
                or entry.dispatchedAt > self.observedAt
            ):
                raise ValueError("invalid quota dispatch time")
            if entry.settledAt is not None and (
                entry.settledAt < entry.reservedAt or entry.settledAt > self.observedAt
                or (entry.dispatchedAt is not None and entry.settledAt < entry.dispatchedAt)
            ):
                raise ValueError("invalid quota settlement time")
        return self


@dataclass(frozen=True)
class Snapshot:
    state: QuotaState
    etag: str
    # Coordination-store time, not a browser timestamp or a replica-local clock.
    now: int


def canonical_digest(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def state_document(state: QuotaState) -> dict:
    doc = state.model_dump(mode="json")
    # Validate copies too: pydantic's model_copy(update=...) does not validate.
    QuotaState.model_validate(doc)
    size = len(json.dumps(doc, ensure_ascii=True).encode("utf-8"))
    if size > MAX_STATE_BYTES:
        raise QuotaError("Hard quota coordination capacity is exhausted.")
    return doc


def ensure_transition_capacity(state: QuotaState) -> None:
    """Reserve wire space for ALL admitted work to finish, not only today's row."""
    future = state_document(state)
    future["observedAt"] = MAX_QUANTITY
    future["replayFloor"] = MAX_QUANTITY
    future["blocked"] = False  # 'false' is longer than 'true'.
    for record in future["entries"].values():
        if record["phase"] not in {"reserved", "dispatched"}:
            continue
        # This is a size envelope, never persisted as a behavioral state. The
        # longest phase/outcome plus maximum legal timestamps/charges covers
        # dispatch, release/expiry, unknown settlement and bound violations.
        record.update(
            phase="dispatched", dispatchedAt=MAX_QUANTITY, settledAt=MAX_QUANTITY,
            outcome="cancelled", settlementDigest="f" * 64,
        )
        record["charged"]["tokens"] = MAX_QUANTITY
        record["charged"]["microUsd"] = MAX_QUANTITY
    if len(json.dumps(future, ensure_ascii=True).encode("utf-8")) > MAX_STATE_BYTES:
        raise QuotaError("Hard quota coordination capacity is exhausted.")


def operation_id(epoch: str, issued_at: int, key: str) -> str:
    result = f"{epoch}.{issued_at}.{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    parse_operation_id(result)
    return result


def parse_operation_id(value: str) -> tuple[str, int]:
    import re

    match = re.fullmatch(r"([0-9a-f]{32})\.([0-9]{1,16})\.([0-9a-f]{64})", value)
    if match is None:
        raise QuotaError("Invalid hard quota operation identity.", code=409)
    return match[1], int(match[2])
