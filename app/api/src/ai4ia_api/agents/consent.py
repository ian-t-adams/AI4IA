"""Bounded, server-owned consent for one session or workflow invocation.

Consent satisfies only the approval gate, never authorization. Callers retain
the normal policy and supply a live checker; snapshots are not bearer tokens.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .tools import ToolSpec

CONSENT_TTL_SECONDS = 8 * 60 * 60
MAX_CONSENT_TOOLS = 128
MAX_CONTRACT_BYTES = 256 * 1024
ApprovalSource = Literal["session", "run", "invocation", "not_required", "operator"]
ConsentStatus = Literal["off", "active", "expired", "revoked", "changed", "disabled", "unavailable"]


class ConsentRejected(RuntimeError):
    """A dispatch preflight observed revocation after the runtime's first check."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in {
            "consent_revoked", "consent_changed", "consent_expired", "consent_disabled",
            "entitlement_denied",
        } else "consent_changed"
        super().__init__(self.reason)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported consent contract value: {type(value).__name__}")


def contract_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=_json_value,
    ).encode("utf-8")
    if len(encoded) > MAX_CONTRACT_BYTES:
        raise ValueError("Tool consent contract is too large.")
    return hashlib.sha256(encoded).hexdigest()


def tool_contract_hash(
    spec: ToolSpec,
    parameters: Mapping[str, Any],
    *,
    description: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    return contract_hash({
        "version": 1,
        "spec": {item.name: getattr(spec, item.name) for item in fields(spec)},
        "parameters": parameters,
        "description": description if description is not None else spec.description,
        "metadata": metadata or {},
    })


class ToolConsentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    scope: Literal["session", "run"]
    grantedAt: datetime
    expiresAt: datetime
    toolCount: int = Field(ge=0, le=MAX_CONSENT_TOOLS)


@dataclass(frozen=True)
class ConsentAssessment:
    consent: ToolConsentSummary | None = None
    active: bool = False
    status: ConsentStatus = "off"


class ToolConsentState(BaseModel):
    """Stored alongside the public summary; never accepted from an HTTP client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: ToolConsentSummary
    userId: str = Field(min_length=1, max_length=256)
    sessionId: str = Field(min_length=1, max_length=256)
    runId: str | None = Field(default=None, max_length=512)
    selectionHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environmentHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    contracts: dict[
        Annotated[str, Field(min_length=1, max_length=64)],
        Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
    ] = Field(max_length=MAX_CONSENT_TOOLS)

    @model_validator(mode="after")
    def coherent_grant(self) -> ToolConsentState:
        if self.grant.toolCount != len(self.contracts):
            raise ValueError("Consent tool count must match its contracts.")
        if (self.grant.scope == "session") != (self.runId is None):
            raise ValueError("Consent scope must match its invocation binding.")
        return self


@dataclass(frozen=True)
class ConsentSnapshot:
    selection_hash: str
    environment_hash: str
    contracts: Mapping[str, str]


def mint_consent(
    snapshot: ConsentSnapshot,
    *,
    user_id: str,
    session_id: str,
    run_id: str | None = None,
    now: datetime | None = None,
) -> ToolConsentState:
    granted = now or datetime.now(timezone.utc)
    if len(snapshot.contracts) > MAX_CONSENT_TOOLS:
        raise ValueError(f"Auto-approval supports at most {MAX_CONSENT_TOOLS} tools.")
    return ToolConsentState(
        grant=ToolConsentSummary(
            id=uuid4().hex,
            scope="run" if run_id is not None else "session",
            grantedAt=granted,
            expiresAt=granted + timedelta(seconds=CONSENT_TTL_SECONDS),
            toolCount=len(snapshot.contracts),
        ),
        userId=user_id,
        sessionId=session_id,
        runId=run_id,
        selectionHash=snapshot.selection_hash,
        environmentHash=snapshot.environment_hash,
        contracts=dict(snapshot.contracts),
    )


@dataclass(frozen=True)
class ConsentDecision:
    approved: bool = False
    scope: Literal["session", "run"] | None = None
    consent_id: str | None = None
    reason: str | None = None


# Invoked afresh at discovery AND immediately before every dispatch. The second
# argument is the contract the current handler/schema actually implements.
ConsentChecker = Callable[[str, str], Awaitable[ConsentDecision]]


def check_consent(
    consent: ToolConsentState,
    snapshot: ConsentSnapshot,
    *,
    tool: str,
    implemented_contract: str,
    now: datetime | None = None,
) -> ConsentDecision:
    grant = consent.grant
    expires = grant.expiresAt
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    granted = grant.grantedAt
    if granted.tzinfo is None:
        granted = granted.replace(tzinfo=timezone.utc)
    reason = None
    if (
        expires <= current or granted > current
        or expires - granted > timedelta(seconds=CONSENT_TTL_SECONDS)
    ):
        reason = "consent_expired"
    elif (
        snapshot.selection_hash != consent.selectionHash
        or snapshot.environment_hash != consent.environmentHash
        or dict(snapshot.contracts) != consent.contracts
    ):
        reason = "consent_changed"
    elif tool not in consent.contracts:
        reason = "consent_not_granted"
    elif (
        consent.contracts[tool] != implemented_contract
        or snapshot.contracts.get(tool) != implemented_contract
    ):
        reason = "consent_changed"
    return ConsentDecision(
        approved=reason is None, scope=grant.scope, consent_id=grant.id, reason=reason,
    )
