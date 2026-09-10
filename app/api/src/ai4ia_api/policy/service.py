"""Current-principal policy composition. Persisted snapshots never supply claims."""
from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from azure.core.exceptions import AzureError

from ..auth.base import AuthenticatedUser
from ..catalog import DeploymentOption, ModelCatalog
from ..entitlements.models import Entitlement, EntitlementLimits
from ..entitlements.service import EntitlementService
from .models import (
    LIMIT_FIELDS, ClaimRule, DomainPolicy, EffectivePolicy, PolicyConfig, PolicyDecision,
    PolicyDomain, PolicyError, PolicyRequest, ResolvedDomain, SpendMapping,
    parse_policy_config, policy_digest,
)

if TYPE_CHECKING:
    from ..config import Settings

_READ_FAILURES = (AzureError, OSError, ValueError, RuntimeError)


def minimum_limits(base: Entitlement, restrictions: Iterable[EntitlementLimits]) -> Entitlement:
    values = list(restrictions)
    changes: dict[str, object] = {"disabled": base.disabled or any(item.disabled for item in values)}
    for name in LIMIT_FIELDS:
        candidates = [
            value for item in (base, *values)
            if (value := getattr(item, name)) is not None
        ]
        changes[name] = min(candidates) if candidates else None
    return base.model_copy(update=changes)


def _expanded(values: Iterable[str], domain: str) -> set[str]:
    if domain != "zones":
        return set(values)
    result: set[str] = set()
    for value in values:
        result.update(
            {"global", "us", "eu"} if value == "global"
            else {"us", "eu"} if value == "zonal"
            else {value}
        )
    return result


def _matched(rule: ClaimRule | SpendMapping, user: AuthenticatedUser | None) -> bool:
    evidence = user.policy_claims if user is not None and user.provider == "entra" else None
    return evidence is not None and rule.value in getattr(evidence, rule.claim)


def _complete(rule: ClaimRule | SpendMapping, user: AuthenticatedUser | None) -> bool:
    evidence = user.policy_claims if user is not None and user.provider == "entra" else None
    return evidence is not None and getattr(evidence, f"{rule.claim}_complete")


def _compose(name: str, domain: DomainPolicy, user: AuthenticatedUser | None) -> ResolvedDomain:
    rules = [domain.default, *(rule for rule in domain.mappings if _matched(rule, user))]
    allowed: set[str] = set()
    denied: set[str] = set()
    restricted: set[str] | None = None
    for rule in rules:
        allowed.update(_expanded(rule.allow, name))
        denied.update(_expanded(rule.deny, name))
    for rule in rules:
        if rule.restrict is not None:
            restriction = _expanded(rule.restrict, name)
            restricted = restriction if restricted is None else restricted & restriction
            allowed.intersection_update(restriction)
    return ResolvedDomain(
        allowed=frozenset(allowed - denied), denied=frozenset(denied),
        restricted=frozenset(restricted) if restricted is not None else None,
        invalid=user is not None and any(not _complete(rule, user) for rule in domain.mappings),
        unavailable=user is None and bool(domain.mappings),
    )


class PolicyService:
    def __init__(
        self, settings: Settings, *, catalog: ModelCatalog, entitlements: EntitlementService,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.entitlements = entitlements
        self._binding = object()
        self._raw: str | None = None
        self._config: PolicyConfig | None = None
        if self.enabled:
            self._configuration()

    @property
    def enabled(self) -> bool:
        return self.settings.group_policy_enabled

    def _configuration(self) -> PolicyConfig:
        raw = self.settings.group_policy_json or ""
        if raw != self._raw or self._config is None:
            try:
                parsed = parse_policy_config(raw)
                domain = parsed.domains.get("models")
                known = {model.category for model in self.catalog.models}
                if domain is not None:
                    for rule in (domain.default, *domain.mappings):
                        if (set(rule.allow) | set(rule.deny) | set(rule.restrict or ())) - known:
                            raise ValueError("Unknown model category in policy.")
            except ValueError as exc:
                raise PolicyError(PolicyDecision("unavailable", "policy_unavailable")) from exc
            self._raw, self._config = raw, parsed
        return self._config

    def allows_model_snapshot(
        self, user: AuthenticatedUser | None, category: str, deployment: DeploymentOption,
    ) -> bool:
        if not self.enabled:
            return True
        config = self._configuration()
        constraints: tuple[tuple[PolicyDomain, str], ...] = (
            ("models", category), ("zones", deployment.residency),
        )
        for name, value in constraints:
            configured = config.domains.get(name)
            if configured is not None:
                domain = _compose(name, configured, user)
                if domain.invalid or domain.unavailable or value not in domain.allowed:
                    return False
        return True

    def allows_tool_snapshot(self, user: AuthenticatedUser | None, name: str) -> bool:
        if not self.enabled:
            return True
        configured = self._configuration().domains.get("tools")
        if configured is None:
            return True
        domain = _compose("tools", configured, user)
        return not domain.invalid and not domain.unavailable and name in domain.allowed

    async def resolve(
        self, user: AuthenticatedUser, *, expected_owner: str | None = None,
    ) -> EffectivePolicy:
        if expected_owner is not None and expected_owner != user.internal_user_id:
            raise PolicyError(PolicyDecision("deny", "owner_mismatch"))
        return await self._resolve(user.internal_user_id, user.model_copy(deep=True))

    async def resolve_unattended(self, owner_id: str) -> EffectivePolicy:
        if not owner_id or len(owner_id) > 256:
            raise PolicyError(PolicyDecision("deny", "owner_mismatch"))
        return await self._resolve(owner_id, None)

    async def _resolve(self, owner: str, user: AuthenticatedUser | None) -> EffectivePolicy:
        empty = EffectivePolicy(
            owner_id=owner, mode="interactive" if user is not None else "unattended",
            digest="off", domains=MappingProxyType({}), limits=Entitlement.unlimited(owner),
            user=user, binding=self._binding,
        )
        if not self.enabled:
            return empty
        config = self._configuration()
        limits = empty.limits
        unavailable = False
        try:
            limits = await self.entitlements.get_for_admission(owner)
        except _READ_FAILURES:
            unavailable = True
        spend_invalid = spend_unattended = False
        if config.spend is not None:
            spend_invalid = user is not None and any(
                not _complete(rule, user) for rule in config.spend.mappings
            )
            spend_unattended = user is None and bool(config.spend.mappings)
            limits = minimum_limits(limits, [
                config.spend.default,
                *(rule.limits for rule in config.spend.mappings if _matched(rule, user)),
            ])
        return replace(
            empty, digest=policy_digest(config), limits=limits,
            limits_unavailable=unavailable, spend_invalid=spend_invalid,
            spend_unattended=spend_unattended,
            domains=MappingProxyType({
                name: _compose(name, domain, user) for name, domain in config.domains.items()
            }),
        )

    def _domain(
        self, policy: EffectivePolicy, name: str, value: str, *, legacy: bool = False,
    ) -> PolicyDecision:
        domain = policy.domains.get(name)
        if domain is None:
            allowed = name != "admin" or legacy
            if name == "publication" and value != "consume":
                allowed = False
            return PolicyDecision("allow" if allowed else "deny", "allowed" if allowed else "policy_denied")
        if domain.unavailable:
            return PolicyDecision("unavailable", "reauthentication_required")
        if domain.invalid:
            return PolicyDecision("deny", "claim_evidence_invalid")
        allowed = value not in domain.denied and (legacy or value in domain.allowed)
        if domain.restricted is not None:
            allowed = allowed and value in domain.restricted
        if name == "admin" and not legacy:
            allowed = allowed and value in self._configuration().adminCeiling
        return PolicyDecision("allow" if allowed else "deny", "allowed" if allowed else "policy_denied")

    def identity_decision(self, policy: EffectivePolicy) -> PolicyDecision | None:
        user = policy.user
        if user is None:
            return None
        if user.internal_user_id != policy.owner_id:
            return PolicyDecision("deny", "owner_mismatch")
        if user.provider == "entra":
            claims = user.policy_claims
            if (
                user.tenant_id not in self.settings.allowed_tenants
                or claims is None or claims.expires_at is None
                or claims.expires_at <= time.time()
            ):
                return PolicyDecision("unavailable", "reauthentication_required")
        elif self.settings.env != "local":
            return PolicyDecision("deny", "claim_evidence_invalid")
        return None

    def decide(self, policy: EffectivePolicy, request: PolicyRequest) -> PolicyDecision:
        """Pure current-snapshot filter for advertisement; dispatch uses authorize."""
        if policy.binding is not self._binding:
            return PolicyDecision("unavailable", "policy_unavailable")
        if not self.enabled:
            return PolicyDecision("allow", "allowed")
        identity = self.identity_decision(policy)
        if identity is not None:
            return identity
        operation = request.operation
        if operation.startswith("admin."):
            return self._domain(policy, "admin", operation, legacy=request.legacy_admin)
        if operation.startswith("publication."):
            if not self.settings.asset_publishing_enabled:
                return PolicyDecision("deny", "feature_disabled")
            return self._domain(policy, "publication", operation.split(".", 1)[1])
        if operation.startswith("document."):
            result = self._domain(policy, "documents", operation.split(".", 1)[1])
            if not result.allowed:
                return result
        if operation == "tool.invoke":
            if not request.tool_name:
                return PolicyDecision("deny", "policy_denied")
            result = self._domain(policy, "tools", request.tool_name)
            if not result.allowed:
                return result
        if operation == "model.invoke":
            entry = self.catalog.get(request.model_id or "")
            deployment = request.deployment
            if entry is None or deployment is None or deployment not in self.catalog.eligible_options(entry):
                return PolicyDecision("deny", "model_unavailable")
            for domain, value in (("models", entry.category), ("zones", deployment.residency)):
                result = self._domain(policy, domain, value)
                if not result.allowed:
                    return result
        if operation in {"model.invoke", "tool.invoke", "document.process", "document.compute"}:
            return self.consumption_state(policy)
        return PolicyDecision("allow", "allowed")

    def consumption_state(self, policy: EffectivePolicy) -> PolicyDecision:
        if policy.binding is not self._binding:
            return PolicyDecision("unavailable", "policy_unavailable")
        identity = self.identity_decision(policy)
        if identity is not None:
            return identity
        if policy.limits.disabled:
            return PolicyDecision("deny", "account_disabled")
        if policy.spend_unattended:
            return PolicyDecision("unavailable", "reauthentication_required")
        if policy.spend_invalid:
            return PolicyDecision("deny", "claim_evidence_invalid")
        if policy.limits_unavailable:
            return PolicyDecision("unavailable", "policy_unavailable")
        return PolicyDecision("allow", "allowed")

    async def authorize(self, policy: EffectivePolicy, request: PolicyRequest) -> PolicyDecision:
        if policy.binding is not self._binding:
            return PolicyDecision("unavailable", "policy_unavailable")
        current = await self._resolve(policy.owner_id, policy.user)
        decision = self.decide(current, request)
        if not decision.allowed or not self.enabled:
            return decision
        if request.operation in {"model.invoke", "tool.invoke", "document.process", "document.compute"}:
            budget = await self.entitlements.check_limits(
                current.owner_id, current.limits,
                scope="compute" if request.operation == "document.compute" else "chat",
                failure_unavailable=True,
            )
            if not budget.allowed:
                return PolicyDecision(
                    "unavailable" if budget.code == 503 else "deny",
                    "policy_unavailable" if budget.code == 503 else "policy_denied",
                    code=budget.code, retry_after_seconds=budget.retry_after_seconds,
                )
            identity = self.identity_decision(current)
            if identity is not None:
                return identity
        return decision

    async def require(self, policy: EffectivePolicy, request: PolicyRequest) -> None:
        decision = await self.authorize(policy, request)
        if not decision.allowed:
            raise PolicyError(decision)
