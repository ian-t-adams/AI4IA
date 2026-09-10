"""Current-principal policy composition. Persisted snapshots never supply claims."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from azure.core.exceptions import AzureError

from ..auth.base import AuthenticatedUser
from ..auth.identity import identity_is_admin
from ..auth.userid import internal_user_id
from ..catalog import DeploymentOption, ModelCatalog
from ..entitlements.models import Entitlement, EntitlementLimits
from ..entitlements.service import EntitlementService
from .models import (
    ADMIN_OPERATIONS, DOCUMENT_TOOL_FEATURES, LIMIT_FIELDS,
    ClaimRule, DomainPolicy, EffectivePolicy, PolicyConfig, PolicyDecision,
    PolicyDomain, PolicyError, PolicyRequest, ResolvedDomain, SpendMapping,
    RestrictedProfile, parse_policy_config, policy_digest,
)

if TYPE_CHECKING:
    from ..config import Settings

_READ_FAILURES = (AzureError, OSError, ValueError, RuntimeError)
CanaryDispatchGuard = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


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
    if evidence is None or not getattr(evidence, f"{rule.claim}_complete"):
        return False
    negative = (
        rule.limits.disabled or rule.limits.has_any_limit
        if isinstance(rule, SpendMapping) else bool(rule.deny) or rule.restrict is not None
    )
    return not negative or getattr(evidence, f"{rule.claim}_present")


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
        canary_guard_provider: Callable[[], CanaryDispatchGuard | None] | None = None,
        evaluation_guard_provider: Callable[[], CanaryDispatchGuard | None] | None = None,
        realtime_guard_provider: Callable[[], CanaryDispatchGuard | None] | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.entitlements = entitlements
        self._binding = object()
        self._canary_guard_provider = canary_guard_provider
        self._evaluation_guard_provider = evaluation_guard_provider
        self._realtime_guard_provider = realtime_guard_provider
        self._raw: str | None = None
        self._config: PolicyConfig | None = None
        if self.enabled or self.settings.group_policy_json:
            self._configuration()

    @property
    def enabled(self) -> bool:
        return self.settings.group_policy_enabled

    def canary_owner(self, *, cached: bool = False) -> str | None:
        return self.profile_owner("monitor-canary", cached=cached)

    def profile_owner(self, profile: RestrictedProfile, *, cached: bool = False) -> str | None:
        # Pausing group evaluation must not turn a still-configured restricted
        # identity into an ordinary unrestricted caller.
        config = self._config if cached or not self.enabled else self._configuration()
        marker = config.actor_for(profile) if config is not None else None
        if marker is None:
            return None
        return internal_user_id(
            provider="entra", issuer=f"https://login.microsoftonline.com/{marker.tenantId}/v2.0",
            subject=marker.subject, tenant_id=marker.tenantId,
        )

    def restricted_profile(self, owner: str, *, cached: bool = False) -> RestrictedProfile | None:
        for profile in ("monitor-canary", "authored-synthetic-evaluation", "realtime-setup-canary"):
            if owner == self.profile_owner(profile, cached=cached):
                return profile
        return None

    def dispatch_guard(self, profile: RestrictedProfile) -> CanaryDispatchGuard | None:
        provider = {
            "monitor-canary": self._canary_guard_provider,
            "authored-synthetic-evaluation": self._evaluation_guard_provider,
            "realtime-setup-canary": self._realtime_guard_provider,
        }[profile]
        return provider() if provider is not None else None

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
        config = self._configuration()
        configured = config.domains.get("tools")
        if configured is not None:
            domain = _compose("tools", configured, user)
            if domain.invalid or domain.unavailable or name not in domain.allowed:
                return False
        feature = DOCUMENT_TOOL_FEATURES.get(name)
        documents = config.domains.get("documents")
        if feature is not None and documents is not None:
            domain = _compose("documents", documents, user)
            return not domain.invalid and not domain.unavailable and feature in domain.allowed
        return True

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
            try:
                limits = await self.entitlements.get_for_admission(owner)
            except _READ_FAILURES:
                return replace(empty, limits_unavailable=True)
            return replace(empty, limits=limits)
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
            if self.restricted_profile(policy.owner_id, cached=True) is not None:
                return PolicyDecision("unavailable", "canary_policy_unconfigured")
            return PolicyDecision("allow", "allowed")
        identity = self.identity_decision(policy)
        if identity is not None:
            return identity
        operation = request.operation
        profile = self.restricted_profile(policy.owner_id)
        if profile == "realtime-setup-canary" and operation != "model.invoke":
            return PolicyDecision("deny", "canary_policy_incompatible")
        if self.restricted_profile(policy.owner_id) is not None and (
            policy.user is None or operation in {"tool.invoke", "document.process", "document.compute"}
        ):
            return PolicyDecision(
                "unavailable" if policy.user is None else "deny",
                "reauthentication_required" if policy.user is None else "canary_policy_incompatible",
            )
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
            feature = DOCUMENT_TOOL_FEATURES.get(request.tool_name)
            if feature is not None:
                result = self._domain(policy, "documents", feature)
                if not result.allowed:
                    return result
        if operation == "model.invoke":
            entry = self.catalog.get(request.model_id or "")
            deployment = request.deployment
            if entry is None or deployment is None or deployment not in self.catalog.eligible_options(entry):
                return PolicyDecision("deny", "model_unavailable")
            if profile == "realtime-setup-canary" and entry.category != "realtime":
                return PolicyDecision("deny", "canary_policy_incompatible")
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

    async def canary_probe(
        self, user: AuthenticatedUser, model_id: str, option: DeploymentOption,
    ) -> PolicyDecision:
        return await self.restricted_probe(user, model_id, option, "monitor-canary")

    async def evaluation_probe(
        self, user: AuthenticatedUser, model_id: str, option: DeploymentOption,
    ) -> PolicyDecision:
        return await self.restricted_probe(user, model_id, option, "authored-synthetic-evaluation")

    async def realtime_canary_probe(
        self, user: AuthenticatedUser, model_id: str, option: DeploymentOption,
    ) -> PolicyDecision:
        return await self.restricted_probe(user, model_id, option, "realtime-setup-canary")

    async def restricted_probe(
        self, user: AuthenticatedUser, model_id: str, option: DeploymentOption,
        profile: RestrictedProfile,
    ) -> PolicyDecision:
        """A readiness restriction, never an identity grant or execution token."""
        if not self.enabled:
            return PolicyDecision("unavailable", "canary_policy_unconfigured")
        config = self._configuration()
        expected = config.actor_for(profile)
        if expected is None:
            return PolicyDecision("unavailable", "canary_policy_unconfigured")
        if profile == "realtime-setup-canary" and (
            not self.settings.realtime_enabled or self.settings.realtime_protocol.value != "ga"
            or "azure_openai" not in self.settings.voice_provider_allowlist_list
        ):
            return PolicyDecision("unavailable", "feature_disabled")
        if (
            user.provider != "entra" or user.tenant_id != expected.tenantId
            or user.subject != expected.subject
            or user.internal_user_id != self.profile_owner(profile)
        ):
            return PolicyDecision("deny", "owner_mismatch")
        actor = await self.resolve(user)
        envelope = self._canary_envelope(actor, profile)
        if not envelope.allowed:
            return envelope
        request = PolicyRequest("model.invoke", model_id=model_id, deployment=option)
        decision = await self.authorize(actor, request)
        if not decision.allowed:
            return decision
        latest = await self.resolve(user)
        if latest.digest != actor.digest or latest.limits != actor.limits:
            return PolicyDecision("unavailable", "canary_policy_incompatible")
        envelope = self._canary_envelope(latest, profile)
        return self.decide(latest, request) if envelope.allowed else envelope

    def _canary_envelope(
        self, actor: EffectivePolicy, profile: RestrictedProfile = "monitor-canary",
    ) -> PolicyDecision:
        user = actor.user
        config = self._configuration()
        expected = config.actor_for(profile)
        if user is None or expected is None or (
            user.provider != "entra" or user.tenant_id != expected.tenantId
            or user.subject != expected.subject
        ):
            return PolicyDecision("deny", "owner_mismatch")
        identity = self.identity_decision(actor)
        if identity is not None:
            return identity
        if user.policy_claims is None or not (
            user.policy_claims.roles_complete and user.policy_claims.groups_complete
        ):
            return PolicyDecision("unavailable", "claim_evidence_invalid")
        if identity_is_admin(user, self.settings) or any(
            self._domain(actor, "admin", operation).allowed for operation in ADMIN_OPERATIONS
        ):
            return PolicyDecision("deny", "canary_policy_incompatible")
        if any(
            self._domain(actor, "publication", operation).allowed for operation in ("submit", "review")
        ):
            return PolicyDecision("deny", "canary_policy_incompatible")
        for name in ("models", "tools", "documents"):
            domain = actor.domains.get(name)
            if domain is None or domain.invalid or domain.unavailable:
                return PolicyDecision("unavailable", "canary_policy_incompatible")
        if actor.domains["tools"].allowed or actor.domains["documents"].allowed:
            return PolicyDecision("deny", "canary_policy_incompatible")
        categories = {entry.category for entry in self.catalog.models}
        if not actor.domains["models"].allowed < categories:
            return PolicyDecision("deny", "canary_policy_incompatible")
        if profile == "realtime-setup-canary" and actor.domains["models"].allowed != {"realtime"}:
            return PolicyDecision("deny", "canary_policy_incompatible")
        if actor.limits_unavailable:
            return PolicyDecision("unavailable", "policy_unavailable")
        if not any(getattr(actor.limits, name) is not None for name in LIMIT_FIELDS[:-1]) or not self.entitlements.enabled:
            return PolicyDecision("deny", "canary_policy_incompatible")
        if self.settings.hard_quota_enabled:
            return PolicyDecision("unavailable", "policy_surface_unsupported")
        return self.consumption_state(actor)

    async def guard_canary_dispatch(
        self, actor: EffectivePolicy, *, surface: str, deployment: str | None,
        payload: dict[str, Any], bound_required: bool = False,
        profile: RestrictedProfile = "monitor-canary",
    ) -> None:
        current_owner = self.profile_owner(profile)
        if not bound_required and actor.owner_id != current_owner:
            return
        if actor.owner_id != current_owner or actor.user is None:
            raise PolicyError(PolicyDecision("unavailable", "canary_policy_unconfigured"))
        expected_surface = "realtime" if profile == "realtime-setup-canary" else "chat"
        if surface != expected_surface or deployment is None:
            raise PolicyError(PolicyDecision("deny", "canary_policy_incompatible"))
        envelope = self._canary_envelope(actor, profile)
        if not envelope.allowed:
            raise PolicyError(envelope)
        guard = self.dispatch_guard(profile)
        if guard is None:
            raise PolicyError(PolicyDecision("unavailable", "canary_policy_incompatible"))
        if await guard(actor.owner_id, deployment, payload) is not True:
            raise PolicyError(PolicyDecision("deny", "canary_policy_incompatible"))
        latest = await self.resolve(actor.user)
        if (
            latest.digest != actor.digest or latest.limits != actor.limits
            or self.profile_owner(profile) != actor.owner_id
        ):
            raise PolicyError(PolicyDecision("unavailable", "canary_policy_incompatible"))
        if self.identity_decision(latest) is not None:
            raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
