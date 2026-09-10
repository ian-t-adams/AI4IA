"""Explicit, catalog-owned production capacity policy; no collection or writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import _capacity_evidence as evidence
from _model_naming import PATTERN, deployment_name

VERSION = "production-capacity-v1"
PROFILES = ("baseline", "production", "maximum")
HASH = re.compile(r"[0-9a-f]{64}\Z")
POOL_FIELDS = frozenset({
    "counter", "unit", "scope", "regions", "model", "capacityUnitsPerCounterUnit",
})
DeploymentKey = tuple[str, str]


def fields(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    result = evidence.object_value(value)
    if not required <= result.keys() or result.keys() - required - (optional or set()):
        raise evidence.EvidenceError("invalid_production_policy_shape")
    return result


def integer(value: object, minimum: int = 0, maximum: int = evidence.MAX_NUMBER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise evidence.EvidenceError("invalid_production_integer")
    return value


def digest(value: object) -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise evidence.EvidenceError("invalid_evidence_digest")
    return value


@dataclass(frozen=True)
class DeploymentPolicy:
    deployment: evidence.Deployment
    pool_id: str
    critical: bool
    critical_minimum: int
    ceiling: int
    capacity: int | None

    @property
    def key(self) -> DeploymentKey:
        return self.deployment.region, self.deployment.name

    @property
    def floor(self) -> int:
        return max(self.deployment.baseline, self.critical_minimum)


@dataclass(frozen=True)
class PoolPolicy:
    id: str
    assertion: dict
    reserve: dict[str, int]
    metric: str
    count_per_capacity_hour: int
    minimum_hours: int
    minimum_total: int

    @property
    def reserved(self) -> int:
        return evidence.number(sum(self.reserve.values()))


@dataclass(frozen=True)
class Policy:
    id: str
    scope: evidence.Scope
    pools: tuple[PoolPolicy, ...]
    deployments: tuple[DeploymentPolicy, ...]
    review: dict | None

    def members(self, pool: PoolPolicy) -> tuple[DeploymentPolicy, ...]:
        return tuple(d for d in self.deployments if d.pool_id == pool.id)

    def enabled(self, include_anthropic: bool) -> set[DeploymentKey]:
        return {
            (d.region, d.name) for d in self.scope.catalog.deployments
            if include_anthropic or d.model.format != "Anthropic"
        }


def _catalog(models: dict, catalog_digest: str) -> evidence.Catalog:
    """Project the same typed identities used by the read-only evidence collector."""
    naming = evidence.object_value(models.get("naming"))
    if (naming.get("pattern") or PATTERN) != PATTERN:
        raise evidence.EvidenceError("unsupported_production_naming_pattern")
    regions = evidence.object_value(models.get("regions"))
    if not 1 <= len(regions) <= evidence.MAX_REGIONS:
        raise evidence.EvidenceError("region_limit_exceeded")
    zones = {}
    for region, metadata in regions.items():
        if not isinstance(region, str) or not evidence.REGION.fullmatch(region):
            raise evidence.EvidenceError("invalid_catalog_region")
        zones[region] = evidence.token(evidence.object_value(metadata).get("dataZone"))
    deployments = []
    seen = set()
    for model in evidence.array(models.get("catalog"), evidence.MAX_MODELS):
        model = evidence.object_value(model)
        name, format_name = evidence.token(model.get("name")), evidence.provider_format(model.get("format"))
        for item in evidence.array(model.get("deployments"), evidence.MAX_DEPLOYMENTS):
            item = evidence.object_value(item)
            region, sku, version = (evidence.token(item.get(k)) for k in ("region", "sku", "version"))
            if region not in zones or sku not in evidence.object_value(naming.get("skuShort")):
                raise evidence.EvidenceError("invalid_catalog_membership")
            try:
                resolved = evidence.token(deployment_name(models, name, item))
            except (KeyError, ValueError, IndexError, AttributeError):
                raise evidence.EvidenceError("invalid_catalog_naming") from None
            key = region, resolved.casefold()
            if key in seen:
                raise evidence.EvidenceError("duplicate_catalog_deployment")
            seen.add(key)
            deployments.append(evidence.Deployment(
                resolved, region, evidence.Model(format_name, name, version, sku),
                integer(item.get("capacity"), 1),
                integer(item["maxCapacity"], 1) if "maxCapacity" in item else None,
                item.get("maxCapacityPool"),
            ))
    if not 1 <= len(deployments) <= evidence.MAX_DEPLOYMENTS:
        raise evidence.EvidenceError("deployment_limit_exceeded")
    if len({(d.model.format, d.model.name, d.model.version) for d in deployments}) > evidence.MAX_MODELS:
        raise evidence.EvidenceError("model_limit_exceeded")
    return evidence.Catalog(zones, evidence.token(naming.get("foundryToken")), tuple(deployments), catalog_digest)


def parse_policy(
    models: dict, *, required: bool = False, include_anthropic: bool = True,
    catalog_digest: str = "",
) -> Policy | None:
    raw_policy = models.get("productionCapacityPolicy")
    has_metadata = any("production" in d for m in models.get("catalog", []) for d in m.get("deployments", []))
    if raw_policy is None:
        if required or has_metadata or "productionCapacityPolicy" in models:
            raise evidence.EvidenceError("production_policy_not_configured")
        return None
    raw = fields(raw_policy, {
        "version", "id", "subscriptionId", "resourceGroup", "environment", "pools",
    }, {"review"})
    if raw["version"] != VERSION:
        raise evidence.EvidenceError("unsupported_production_policy_version")
    catalog = _catalog(models, catalog_digest)
    scope = evidence.Scope(
        evidence.subscription_id(raw["subscriptionId"]),
        evidence.token(raw["resourceGroup"]), evidence.token(raw["environment"]), catalog,
    )
    policy_id = evidence.token(raw["id"])
    review = None
    if "review" in raw:
        review = fields(raw["review"], {
            "reference", "reviewedAt", "reportSha256", "catalogSha256", "poolEvidenceSha256",
        })
        evidence.token(review["reference"])
        evidence.parse_utc(review["reviewedAt"])
        for key in ("reportSha256", "catalogSha256", "poolEvidenceSha256"):
            digest(review[key])
    if required and review is None:
        raise evidence.EvidenceError("production_review_missing")
    if required and review is not None and evidence.parse_utc(review["reviewedAt"]) > datetime.now(UTC):
        raise evidence.EvidenceError("production_review_in_future")
    raw_pools = evidence.array(raw["pools"], evidence.MAX_MODELS)
    assertions, pools = [], []
    ids = set()
    for item in raw_pools:
        item = fields(item, {"id", "pool", "reserve", "usage"})
        pool_id = evidence.token(item["id"])
        if pool_id in ids:
            raise evidence.EvidenceError("duplicate_production_pool")
        ids.add(pool_id)
        reserve = fields(item["reserve"], {"replacement", "retry", "otherWorkloads"})
        reserve = {key: integer(value) for key, value in reserve.items()}
        if not evidence.number(sum(reserve.values())):
            raise evidence.EvidenceError("production_reserve_required")
        usage = fields(item["usage"], {"metric", "countPerCapacityHour", "minimumHours", "minimumTotal"})
        if usage["metric"] not in evidence.METRICS:
            raise evidence.EvidenceError("unsupported_production_usage_metric")
        assertion = fields(item["pool"], set(POOL_FIELDS))
        assertions.append(assertion)
        pools.append(PoolPolicy(
            pool_id, assertion, reserve, usage["metric"],
            integer(usage["countPerCapacityHour"], 1),
            integer(usage["minimumHours"], 24, evidence.MAX_POINTS),
            integer(usage["minimumTotal"], 1),
        ))
    # This validates durable policy shape/membership, not freshness or live authority.
    # Live report assertions are validated again against the actual observation time.
    shape_time = datetime(2000, 1, 1, tzinfo=UTC)
    normalized = evidence.parse_pool_evidence({
        "schemaVersion": 1, "subscriptionId": scope.subscription,
        "observedAt": evidence.utc_text(shape_time), "reference": policy_id, "pools": assertions,
    }, scope, shape_time)
    pools = [
        PoolPolicy(p.id, assertion, p.reserve, p.metric, p.count_per_capacity_hour, p.minimum_hours, p.minimum_total)
        for p, assertion in zip(pools, normalized["pools"], strict=True)
    ]
    by_id = {p.id: p for p in pools}
    by_key = {(d.region, d.name): d for d in catalog.deployments}
    deployments = []
    for model in models["catalog"]:
        for item in model["deployments"]:
            name = deployment_name(models, model["name"], item)
            deployment = by_key[(item["region"], name)]
            enabled = include_anthropic or deployment.model.format != "Anthropic"
            if "production" not in item:
                if required and enabled:
                    raise evidence.EvidenceError("production_deployment_not_configured")
                continue
            config = fields(item["production"], {"poolId", "critical", "criticalMinimum", "ceiling"}, {"capacity"})
            pool_id = evidence.token(config["poolId"])
            if pool_id not in by_id or type(config["critical"]) is not bool:
                raise evidence.EvidenceError("invalid_production_deployment")
            pool = by_id[pool_id].assertion
            identity = pool["model"]
            if (
                deployment.region not in pool["regions"]
                or (deployment.model.format, deployment.model.name, deployment.model.sku)
                != (identity["format"], identity["name"], identity["sku"])
                or deployment.model.version not in identity["versions"]
            ):
                raise evidence.EvidenceError("production_pool_membership_mismatch")
            critical_minimum = integer(config["criticalMinimum"], int(config["critical"]))
            if not config["critical"] and critical_minimum != 0:
                raise evidence.EvidenceError("noncritical_minimum_must_be_zero")
            ceiling = integer(config["ceiling"], max(deployment.baseline, critical_minimum))
            selected = integer(config["capacity"], max(deployment.baseline, critical_minimum), ceiling) if "capacity" in config else None
            if required and enabled and selected is None:
                raise evidence.EvidenceError("production_capacity_missing")
            deployments.append(DeploymentPolicy(deployment, pool_id, config["critical"], critical_minimum, ceiling, selected))
    result = Policy(policy_id, scope, tuple(pools), tuple(deployments), review)
    if required:
        for pool in result.pools:
            selected = [d for d in result.members(pool) if d.key in result.enabled(include_anthropic)]
            if selected:
                replacement = max((d.capacity or 0 for d in selected), default=0)
                if pool.reserve["replacement"] < replacement:
                    raise evidence.EvidenceError("production_replacement_reserve_too_small")
    return result


def effective_capacity(deployment: dict, profile: str) -> int:
    if profile not in PROFILES:
        raise evidence.EvidenceError("invalid_capacity_profile")
    if profile == "production":
        config = evidence.object_value(deployment.get("production"))
        if "capacity" not in config:
            raise evidence.EvidenceError("production_capacity_missing")
        return integer(config["capacity"], 1)
    return (
        deployment.get("maxCapacity", deployment.get("capacity", 0))
        if profile == "maximum" else deployment.get("capacity", 0)
    )


def bind_scope(policy: Policy, subscription: str, resource_group: str, environment: str) -> None:
    if (
        evidence.subscription_id(subscription) != policy.scope.subscription
        or resource_group != policy.scope.resource_group or environment != policy.scope.environment
    ):
        raise evidence.EvidenceError("production_scope_mismatch")


def budget(
    pool: PoolPolicy, members: tuple[DeploymentPolicy, ...], targets: dict[DeploymentKey, int],
    *, current: int, limit: int, catalog_allocation: int,
) -> dict:
    current, limit, catalog_allocation = (evidence.number(v) for v in (current, limit, catalog_allocation))
    if catalog_allocation > current or current > limit:
        raise evidence.EvidenceError("contradictory_pool_allocation")
    for member in members:
        integer(targets[member.key], member.floor, member.ceiling)
    replacement = max((targets[d.key] for d in members), default=0)
    if pool.reserve["replacement"] < replacement:
        raise evidence.EvidenceError("production_replacement_reserve_too_small")
    proposed = evidence.number(sum(targets.values()))
    outside = current - catalog_allocation
    allocated = evidence.number(proposed + outside)
    required = evidence.number(allocated + pool.reserved)
    if required > limit:
        raise evidence.EvidenceError("production_pool_reserve_exceeded")
    return {
        "authority": "operator_asserted", "unit": pool.assertion["unit"],
        "counterLimit": limit, "counterCurrentValue": current,
        "outsideCatalogOrUnattributedAllocation": outside,
        "currentCatalogAllocation": catalog_allocation, "proposedCatalogAllocation": proposed,
        "headroomBefore": limit - current, "headroomAfter": limit - allocated,
        "reserve": dict(pool.reserve), "unreservedHeadroomAfter": limit - required,
    }


def check_increase_headroom(
    pool: PoolPolicy, targets: dict[DeploymentKey, int], live: dict[DeploymentKey, int],
    current: int, limit: int,
) -> int:
    increase = evidence.number(sum(max(0, target - live[key]) for key, target in targets.items()))
    if increase and evidence.number(current + increase + pool.reserved) > limit:
        raise evidence.EvidenceError("replacement_order_headroom_insufficient")
    return increase


def check_live_pools(
    policy: Policy, inventory: dict[tuple[str, str], dict[str, Any]],
    quotas: dict[str, list[dict]], *, include_anthropic: bool,
) -> list[dict]:
    """Recheck exact asserted counters, all versions and outside allocations.

    This consumes the normal preflight's reads. It does not discover pool identity
    or claim that quota headroom is currently allocatable platform capacity.
    """
    results = []
    enabled = policy.enabled(include_anthropic)
    for pool in policy.pools:
        members = policy.members(pool)
        if not any(d.key in enabled for d in members):
            continue
        assertion = pool.assertion
        values = set()
        for region in assertion["regions"]:
            matches = [q for q in quotas.get(region, []) if q["counter"] == assertion["counter"]]
            if len(matches) != 1 or matches[0]["unit"] != assertion["unit"]:
                raise evidence.EvidenceError("pool_counter_or_unit_mismatch")
            values.add((evidence.number(matches[0]["currentValue"]), evidence.number(matches[0]["limit"])))
        if len(values) != 1:
            raise evidence.EvidenceError("contradictory_pool_counters")
        current, limit = values.pop()
        identity = assertion["model"]
        known = 0
        for row in inventory.values():
            if row["region"] not in assertion["regions"]:
                continue
            if (row["format"], row["modelName"], row["sku"]) != (identity["format"], identity["name"], identity["sku"]):
                continue
            if not evidence.owned_account_name(policy.scope, row["region"], row["accountName"]):
                raise evidence.EvidenceError("production_account_mismatch")
            if row["version"] not in identity["versions"] or row["provisioningState"] != "Succeeded":
                raise evidence.EvidenceError("unsettled_or_unreviewed_pool_deployment")
            known = evidence.number(known + integer(row["capacity"], 1))
        if known > current:
            raise evidence.EvidenceError("contradictory_pool_allocation")
        targets = {}
        live_capacities = {}
        catalog_allocation = 0
        expected = [
            d for d in policy.scope.catalog.deployments
            if d.region in assertion["regions"]
            and (d.model.format, d.model.name, d.model.sku) == (identity["format"], identity["name"], identity["sku"])
        ]
        configured = {d.key: d for d in members}
        for deployment in expected:
            key = deployment.region, deployment.name
            row = inventory.get((deployment.region, deployment.name.casefold()))
            if row is None or (
                row["deploymentName"], row["format"], row["modelName"], row["version"], row["sku"], row["provisioningState"]
            ) != (
                deployment.name, deployment.model.format, deployment.model.name,
                deployment.model.version, deployment.model.sku, "Succeeded",
            ):
                raise evidence.EvidenceError("production_inventory_incomplete")
            if not evidence.owned_account_name(policy.scope, deployment.region, row["accountName"]):
                raise evidence.EvidenceError("production_account_mismatch")
            live = integer(row["capacity"], 1)
            live_capacities[key] = live
            catalog_allocation = evidence.number(catalog_allocation + live)
            if key in enabled:
                config = configured.get(key)
                if config is None or config.capacity is None:
                    raise evidence.EvidenceError("production_capacity_missing")
                targets[key] = config.capacity
            else:
                # Disabled catalog entries are not deleted by an ARM reconciliation.
                targets[key] = live
        active = tuple(d for d in members if d.key in enabled)
        check_increase_headroom(pool, targets, live_capacities, current, limit)
        results.append({
            "id": pool.id, "counter": assertion["counter"], "scope": assertion["scope"],
            **budget(pool, active, targets, current=current, limit=limit, catalog_allocation=catalog_allocation),
            "platformAvailability": "not_evaluated",
        })
    return results
