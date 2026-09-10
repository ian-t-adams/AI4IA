"""Strict configuration and non-serializable, owner-bound policy decisions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..auth.policy_claims import GROUP_ID
from ..catalog import DeploymentOption
from ..entitlements.models import Entitlement, EntitlementLimits

if TYPE_CHECKING:
    from ..auth.base import AuthenticatedUser

MAX_POLICY_BYTES = 64 * 1024
MAX_MAPPINGS = 128
PolicyDomain = Literal["models", "zones", "tools", "documents", "publication", "admin"]
PolicyOutcome = Literal["allow", "deny", "unavailable"]
PolicyReason = Literal[
    "allowed", "policy_denied", "policy_unavailable", "reauthentication_required",
    "claim_evidence_invalid", "owner_mismatch", "account_disabled", "feature_disabled",
    "model_unavailable", "policy_surface_unsupported",
]
DOCUMENT_FEATURES = frozenset({
    "read", "upload", "process", "compute", "export", "share", "annotate",
    "memory", "analyzers", "index",
})
PUBLICATION_ACTIONS = frozenset({"submit", "review", "consume"})
ADMIN_OPERATIONS: frozenset[PolicyOperation] = frozenset({
    "admin.usage.read", "admin.directory.read", "admin.entitlements.read",
    "admin.entitlements.write", "admin.metrics.resources.read",
    "admin.metrics.operations.read", "admin.metrics.security.read",
    "admin.metrics.websearch.read", "admin.mcp.inspect", "admin.mcp.refresh",
})
PolicyOperation = Literal[
    "model.invoke", "tool.invoke",
    "document.read", "document.upload", "document.process", "document.compute",
    "document.export", "document.share", "document.annotate", "document.memory",
    "document.analyzers", "document.index",
    "publication.submit", "publication.review", "publication.consume",
    "admin.usage.read", "admin.directory.read", "admin.entitlements.read",
    "admin.entitlements.write", "admin.metrics.resources.read",
    "admin.metrics.operations.read", "admin.metrics.security.read",
    "admin.metrics.websearch.read", "admin.mcp.inspect", "admin.mcp.refresh",
]
LIMIT_FIELDS = (
    "requestsPerMinute", "tokensPerDay", "costPerDayMicroUsd",
    "tokensPerMonth", "costPerMonthMicroUsd", "computeExecutionsPerDay",
)
PolicyValue = Annotated[str, Field(min_length=1, max_length=256, strict=True)]
ValueSet = Annotated[tuple[PolicyValue, ...], Field(max_length=128)]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DomainRule(StrictRecord):
    allow: ValueSet = ()
    deny: ValueSet = ()
    restrict: ValueSet | None = None

    @field_validator("allow", "deny", "restrict")
    @classmethod
    def exact_values(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is not None and (
            len(set(values)) != len(values)
            or any(value != value.strip() or not value.isprintable() or "*" in value for value in values)
        ):
            raise ValueError("Policy values must be unique, exact printable identifiers.")
        return values


class ClaimRule(DomainRule):
    claim: Literal["roles", "groups"]
    value: PolicyValue

    @model_validator(mode="after")
    def exact_claim(self) -> ClaimRule:
        if (
            self.value != self.value.strip() or not self.value.isprintable()
            or (self.claim == "groups" and GROUP_ID.fullmatch(self.value) is None)
        ):
            raise ValueError("Claim mapping requires an exact role value or group object ID.")
        return self


class DomainPolicy(StrictRecord):
    default: DomainRule
    mappings: tuple[ClaimRule, ...] = Field(default=(), max_length=MAX_MAPPINGS)


class SpendLimits(EntitlementLimits):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SpendMapping(StrictRecord):
    claim: Literal["roles", "groups"]
    value: PolicyValue
    limits: SpendLimits

    @model_validator(mode="after")
    def exact_claim(self) -> SpendMapping:
        ClaimRule(claim=self.claim, value=self.value)
        return self


class SpendPolicy(StrictRecord):
    default: SpendLimits
    mappings: tuple[SpendMapping, ...] = Field(default=(), max_length=MAX_MAPPINGS)


class PolicyConfig(StrictRecord):
    version: Literal[1] = 1
    domains: dict[PolicyDomain, DomainPolicy] = Field(default_factory=dict, max_length=6)
    spend: SpendPolicy | None = None
    adminCeiling: ValueSet = ()

    @model_validator(mode="after")
    def bounded_known_configuration(self) -> PolicyConfig:
        count = sum(len(domain.mappings) for domain in self.domains.values())
        if self.spend is not None:
            count += len(self.spend.mappings)
        if count > MAX_MAPPINGS:
            raise ValueError("Too many policy mappings.")
        if len(set(self.adminCeiling)) != len(self.adminCeiling) or set(self.adminCeiling) - ADMIN_OPERATIONS:
            raise ValueError("Unknown or duplicate mapped admin ceiling operation.")
        known = {
            "zones": frozenset({"global", "zonal", "us", "eu"}),
            "documents": DOCUMENT_FEATURES,
            "publication": PUBLICATION_ACTIONS,
            "admin": ADMIN_OPERATIONS,
        }
        for name, domain in self.domains.items():
            identities = [(item.claim, item.value) for item in domain.mappings]
            if len(set(identities)) != len(identities):
                raise ValueError("Duplicate claim mapping in a policy domain.")
            for rule in (domain.default, *domain.mappings):
                values = set(rule.allow) | set(rule.deny) | set(rule.restrict or ())
                if name in known and values - known[name]:
                    raise ValueError(f"Unknown value in policy domain {name}.")
        if self.spend is not None:
            identities = [(item.claim, item.value) for item in self.spend.mappings]
            if len(set(identities)) != len(identities):
                raise ValueError("Duplicate spend claim mapping.")
        return self


def parse_policy_config(raw: str) -> PolicyConfig:
    if not raw or len(raw.encode("utf-8")) > MAX_POLICY_BYTES:
        raise ValueError("Policy configuration is missing or exceeds 64 KiB.")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate policy JSON key.")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("Non-finite policy values are invalid.")

    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    return PolicyConfig.model_validate(value)


def policy_digest(config: PolicyConfig) -> str:
    return hashlib.sha256(json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ResolvedDomain:
    allowed: frozenset[str]
    denied: frozenset[str]
    restricted: frozenset[str] | None = None
    invalid: bool = False
    unavailable: bool = False


@dataclass(frozen=True)
class EffectivePolicy:
    owner_id: str
    mode: Literal["interactive", "unattended"]
    digest: str
    domains: Mapping[str, ResolvedDomain]
    limits: Entitlement
    limits_unavailable: bool = False
    spend_invalid: bool = False
    spend_unattended: bool = False
    user: AuthenticatedUser | None = field(default=None, repr=False, compare=False)
    binding: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PolicyRequest:
    operation: PolicyOperation
    model_id: str | None = None
    deployment: DeploymentOption | None = None
    tool_name: str | None = None
    tool_contract_digest: str | None = None
    resource_ids: tuple[str, ...] = ()
    legacy_admin: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: PolicyReason
    code: int | None = None
    retry_after_seconds: int | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


class PolicyError(RuntimeError):
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        self.status_code = decision.code or (503 if decision.outcome == "unavailable" else 403)
        super().__init__(decision.reason)
