"""Bounded, credential-free rolling-admission persistence contract."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..entitlements.models import MONTH_SECONDS

STATE_ID = "hard-quota-state-v1"
STATE_KIND = "hard_quota_state"
POLICY_VERSION = "rolling-dispatch-v1"
MAX_ENTRIES = 1024
MAX_STATE_BYTES = 512 * 1024
REPLAY_SECONDS = MONTH_SECONDS
RESERVATION_SECONDS = 120
MAX_ADMISSION_EVIDENCE = 8

Count = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Epoch = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Owner = Annotated[str, Field(min_length=1, max_length=128)]
Surface = Literal[
    "chat", "embedding", "image", "video", "transcription", "speech",
    "realtime", "compute", "document", "web_search", "mcp", "external_tool",
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
            if key != entry.operationId:
                raise ValueError("invalid quota operation key")
            epoch, issued = parse_operation_id(key)
            if epoch != self.epoch or issued < self.validAfter:
                raise ValueError("invalid quota operation scope")
            if entry.expiresAt < entry.reservedAt:
                raise ValueError("invalid quota reservation lease")
            if entry.phase == "reserved" and entry.dispatchedAt is not None:
                raise ValueError("reserved operation was already dispatched")
            if entry.phase in {"dispatched", "settled", "unknown"} and entry.dispatchedAt is None:
                raise ValueError("missing quota dispatch evidence")
            if entry.phase in {"settled", "unknown", "released"} and entry.settledAt is None:
                raise ValueError("missing quota settlement evidence")
            if entry.phase == "released" and entry.charged != Amounts.zero():
                raise ValueError("released operation retains a charge")
            if entry.protected and entry.charged != entry.bounds.amounts:
                raise ValueError("unsettled or unknown reservation lost its bound")
            if entry.reservedAt > self.observedAt:
                raise ValueError("quota reservation is ahead of the coordination clock")
            if entry.settledAt is not None and (
                entry.settledAt < entry.reservedAt or entry.settledAt > self.observedAt
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
