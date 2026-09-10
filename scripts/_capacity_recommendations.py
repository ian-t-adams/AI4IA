"""Offline recommendations from bounded capacity reports and explicit catalog policy."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import _capacity_evidence as evidence
from _production_capacity import (
    POOL_FIELDS,
    VERSION,
    DeploymentKey,
    DeploymentPolicy,
    Policy,
    PoolPolicy,
    budget,
    check_increase_headroom,
    digest,
    fields,
    integer,
    parse_policy,
)

REPORT_VERSION = "production-recommendations-v1"
MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class Snapshot:
    report_digest: str
    pool_digest: str | None
    finished_at: str
    window: evidence.Window
    sources: dict[str, dict]
    deployments: dict[DeploymentKey, dict]
    pools: list[dict]
    pool_mismatches: set[tuple[str, str]]


def load_policy(path: Path) -> Policy:
    document, catalog_digest = evidence.read_json(path, evidence.MAX_CATALOG_BYTES)
    policy = parse_policy(evidence.object_value(document), catalog_digest=catalog_digest)
    if policy is None:
        raise evidence.EvidenceError("production_policy_not_configured")
    return policy


def _model(value: object) -> evidence.Model:
    raw = fields(value, {"format", "name", "version", "sku"})
    return evidence.Model(
        evidence.provider_format(raw["format"]), evidence.token(raw["name"]),
        evidence.token(raw["version"]), evidence.token(raw["sku"]),
    )


def _sources(report: dict, scope: evidence.Scope, start: datetime, end: datetime) -> dict[str, dict]:
    expected = {"group": ("group", {}), "accounts": ("accounts", {})}
    for region in scope.catalog.regions:
        for operation in ("deployments", "quota", "definitions", "metrics"):
            expected[f"{operation}:{region}"] = (operation, {"region": region})
    for deployment in scope.catalog.deployments:
        model = deployment.model
        key = model.format, model.name, model.version
        expected[evidence.availability_source_id(key)] = (
            "availability", {"format": model.format, "name": model.name, "version": model.version},
        )
    result = {}
    for raw in evidence.array(report.get("sources"), evidence.MAX_CALLS):
        row = evidence.object_value(raw)
        source_id = row.get("id")
        if not isinstance(source_id, str) or source_id not in expected or source_id in result:
            raise evidence.EvidenceError("invalid_report_source")
        operation, target = expected[source_id]
        actual_target = dict(evidence.object_value(row.get("target")))
        account = actual_target.pop("account", None)
        if row.get("operation") != operation or actual_target != target:
            raise evidence.EvidenceError("report_source_scope_mismatch")
        if account is not None and (
            operation not in {"deployments", "definitions", "metrics"}
            or not evidence.owned_account_name(scope, target["region"], account)
        ):
            raise evidence.EvidenceError("report_source_scope_mismatch")
        finished = evidence.parse_utc(row.get("finishedAt"))
        started = evidence.parse_utc(row["startedAt"]) if row.get("startedAt") is not None else finished
        if not start <= started <= finished <= end:
            raise evidence.EvidenceError("report_source_time_mismatch")
        codes = evidence.array(row.get("codes"), 64)
        for code in codes:
            evidence.token(code)
        if row.get("status") not in {"available", "partial", "unavailable"} or type(row.get("attempted")) is not bool:
            raise evidence.EvidenceError("invalid_report_source")
        if row["status"] == "available" and (
            codes or not row["attempted"]
            or (operation in {"deployments", "definitions", "metrics"} and account is None)
        ):
            raise evidence.EvidenceError("invalid_report_source")
        result[source_id] = row
    return result


def _inventory(
    report: dict, scope: evidence.Scope, sources: dict[str, dict],
) -> tuple[dict[DeploymentKey, dict], dict[str, list[dict]]]:
    expected = {(d.region, d.name): d for d in scope.catalog.deployments}
    declared, inventories = {}, defaultdict(list)
    seen = set()
    for raw in evidence.array(report.get("deployments"), evidence.MAX_DEPLOYMENTS):
        row = evidence.object_value(raw)
        declaration = evidence.object_value(row.get("catalog"))
        key = evidence.token(declaration.get("region")), evidence.token(declaration.get("name"))
        if key not in expected or key in declared or declaration != expected[key].public():
            raise evidence.EvidenceError("report_catalog_identity_mismatch")
        region, name = key
        if row.get("allocationSource") != f"deployments:{region}":
            raise evidence.EvidenceError("report_allocation_source_mismatch")
        source = sources.get(row["allocationSource"], {})
        live = row.get("live")
        if live is not None:
            live = evidence.object_value(live)
            if (
                live.get("unit") != "raw_capacity_units"
                or not evidence.owned_account_name(scope, region, row.get("account"))
                or row["account"] != source.get("target", {}).get("account")
                or live.get("retrievedAt") != source.get("finishedAt")
            ):
                raise evidence.EvidenceError("report_allocation_scope_mismatch")
            model = _model(live.get("model"))
            state = evidence.token(live.get("provisioningState"))
            inventories[region].append({
                "name": name, "model": model, "capacity": integer(live.get("capacity"), 1), "state": state,
            })
            inventory_status = (
                "unknown" if source.get("status") != "available"
                else "identity_mismatch" if model != expected[key].model
                else "not_succeeded" if state != "Succeeded" else "matched"
            )
        else:
            inventory_status = "absent" if source.get("status") == "available" else "unknown"
        if row.get("inventoryStatus") != inventory_status:
            raise evidence.EvidenceError("inconsistent_report_inventory")
        declared[key] = row
        seen.add((region, name.casefold()))
    if declared.keys() != expected.keys():
        raise evidence.EvidenceError("incomplete_report_catalog")
    for raw in evidence.array(report.get("uncataloguedDeployments"), evidence.MAX_DEPLOYMENTS):
        row = evidence.object_value(raw)
        region, name = evidence.token(row.get("region")), evidence.token(row.get("name"))
        key = region, name.casefold()
        if (
            region not in scope.catalog.regions or key in seen
            or row.get("unit") != "raw_capacity_units"
            or row.get("allocationSource") != f"deployments:{region}"
            or not evidence.owned_account_name(scope, region, row.get("account"))
            or row["account"] != sources.get(row["allocationSource"], {}).get("target", {}).get("account")
        ):
            raise evidence.EvidenceError("report_allocation_scope_mismatch")
        seen.add(key)
        if len(seen) > evidence.MAX_DEPLOYMENTS:
            raise evidence.EvidenceError("deployment_limit_exceeded")
        # v1 reports do not retain an uncatalogued deployment's provisioning state.
        # It cannot be manufactured here to turn incomplete allocation into success.
        inventories[region].append({
            "name": name, "model": _model(row.get("model")),
            "capacity": integer(row.get("capacity"), 1), "state": "Unknown",
        })
    return declared, dict(inventories)


def _quotas(report: dict, scope: evidence.Scope, sources: dict[str, dict]) -> dict[str, list[dict]]:
    raw_by_region = defaultdict(list)
    seen = set()
    for raw in evidence.array(report.get("quotaCounters"), evidence.MAX_ROWS):
        group = evidence.object_value(raw)
        counter, unit = evidence.token(group.get("counter")), evidence.token(group.get("unit"))
        if group.get("scope") != "unknown" or group.get("headroom") is not None:
            raise evidence.EvidenceError("invalid_raw_quota_observation")
        for raw_observation in evidence.array(group.get("observations"), evidence.MAX_REGIONS):
            observation = evidence.object_value(raw_observation)
            region = evidence.token(observation.get("region"))
            key = region, counter.casefold()
            source_id = f"quota:{region}"
            source = sources.get(source_id, {})
            if (
                region not in scope.catalog.regions or key in seen
                or observation.get("source") != source_id
                or observation.get("retrievedAt") != source.get("finishedAt")
                or observation.get("sourceStatus") != source.get("status")
            ):
                raise evidence.EvidenceError("report_quota_source_mismatch")
            seen.add(key)
            raw_by_region[region].append({
                "name": {"value": counter}, "unit": unit,
                "currentValue": observation.get("currentValue"), "limit": observation.get("limit"),
            })
    return {region: evidence.parse_quota({"value": rows}) for region, rows in raw_by_region.items()}


def _availability(report: dict, scope: evidence.Scope, sources: dict[str, dict]) -> dict[tuple[str, str, str], list[dict]]:
    grouped = defaultdict(list)
    expected = {
        (d.model.format, d.model.name, d.model.version): d.model for d in scope.catalog.deployments
    }
    for raw in evidence.array(report.get("platformAvailability"), evidence.MAX_MODELS * evidence.MAX_REGIONS * 5):
        row = evidence.object_value(raw)
        model = fields(row.get("model"), {"format", "name", "version"})
        key = evidence.provider_format(model["format"]), evidence.token(model["name"]), evidence.token(model["version"])
        source_id = evidence.availability_source_id(key)
        if (
            key not in expected or row.get("source") != source_id
            or row.get("retrievedAt") != sources.get(source_id, {}).get("finishedAt")
            or row.get("unit") != "raw_capacity_units"
            or row.get("region") not in scope.catalog.regions
        ):
            raise evidence.EvidenceError("report_availability_source_mismatch")
        grouped[key].append({
            "location": row["region"],
            "properties": {"model": model, "skuName": row.get("sku"), "availableCapacity": row.get("availableCapacity")},
        })
    return {
        key: evidence.parse_availability({"value": rows}, expected[key], scope.catalog)
        for key, rows in grouped.items()
    }


def load_snapshot(path: Path, policy: Policy, now: datetime) -> Snapshot:
    raw, report_digest = evidence.read_json(path, evidence.MAX_REPORT_BYTES)
    report = evidence.object_value(raw)
    if (
        type(report.get("schemaVersion")) is not int or report["schemaVersion"] != 1
        or report.get("status") not in {"complete", "partial"}
        or report.get("writes") != "none" or report.get("policy") != "not_evaluated"
        or report.get("recommendations") != []
    ):
        raise evidence.EvidenceError("unsupported_capacity_report")
    if (
        evidence.object_value(report.get("catalog")).get("sha256") != policy.scope.catalog.digest
        or report.get("scope") != policy.scope.public()
    ):
        raise evidence.EvidenceError("report_catalog_or_scope_mismatch")
    start, end = evidence.parse_utc(report.get("startedAt")), evidence.parse_utc(report.get("finishedAt"))
    if not now - MAX_AGE <= start <= end <= now:
        raise evidence.EvidenceError("stale_capacity_report")
    window_raw = evidence.object_value(report.get("window"))
    window_start = evidence.parse_utc(window_raw.get("start"))
    window_end = evidence.parse_utc(window_raw.get("endExclusive"))
    hours = (window_end - window_start).total_seconds() / 3600
    if hours not in range(24, evidence.MAX_POINTS + 1, 24):
        raise evidence.EvidenceError("invalid_metric_window")
    window = evidence.Window.create(int(hours) // 24, window_raw.get("endExclusive"), start)
    if window_raw != window.public():
        raise evidence.EvidenceError("invalid_metric_window")
    sources = _sources(report, policy.scope, start, end)
    deployments, inventories = _inventory(report, policy.scope, sources)
    quotas = _quotas(report, policy.scope, sources)
    availability = _availability(report, policy.scope, sources)
    pool_metadata = evidence.object_value(report.get("poolEvidence"))
    raw_pools = evidence.array(report.get("pools"), evidence.MAX_MODELS)
    pool_digest, assertions = None, None
    if pool_metadata.get("authority") == "operator_asserted":
        pool_digest = digest(pool_metadata.get("sha256"))
        if pool_metadata.get("error") is not None:
            raise evidence.EvidenceError("invalid_pool_evidence")
        assertions = evidence.parse_pool_evidence({
            "schemaVersion": 1, "subscriptionId": policy.scope.subscription,
            "observedAt": pool_metadata.get("observedAt"), "reference": pool_metadata.get("reference"),
            "pools": [{k: evidence.object_value(row).get(k) for k in POOL_FIELDS} for row in raw_pools],
        }, policy.scope, now)
        if evidence.parse_utc(assertions["observedAt"]) > end:
            raise evidence.EvidenceError("report_pool_time_mismatch")
    elif pool_metadata.get("authority") != "not_established" or raw_pools:
        raise evidence.EvidenceError("invalid_pool_evidence")
    recomputed, _coverage = evidence.pool_rollups(
        assertions, policy.scope.catalog, inventories, quotas, availability, sources, None,
    )
    mismatches = set()
    for actual, claimed in zip(recomputed, raw_pools, strict=True):
        if actual != claimed:
            mismatches.add((actual["counter"], actual["scope"]))
    return Snapshot(report_digest, pool_digest, evidence.utc_text(end), window, sources, deployments, recomputed, mismatches)


def _usage_target(member: DeploymentPolicy, pool: PoolPolicy, snapshot: Snapshot) -> int:
    row = snapshot.deployments[member.key]
    if row["inventoryStatus"] != "matched":
        raise evidence.EvidenceError("incomplete_catalog_inventory")
    region = member.deployment.region
    for source_id in (f"metrics:{region}", f"definitions:{region}"):
        source = snapshot.sources.get(source_id, {})
        if source.get("status") != "available" or source.get("codes"):
            raise evidence.EvidenceError("incomplete_usage_sources")
    values = evidence.object_value(row.get("usage"))
    if values.keys() != set(evidence.METRICS):
        raise evidence.EvidenceError("incomplete_usage")
    for raw in values.values():
        value = evidence.object_value(raw)
        if (
            value.get("status") != "measured" or value.get("codes") != []
            or value.get("unit") != "Count" or value.get("source") != f"metrics:{region}"
            or value.get("coverage") != "returned_series_only"
        ):
            raise evidence.EvidenceError("incomplete_usage")
        series = integer(value.get("series"), 1, evidence.MAX_SERIES)
        samples = integer(value.get("samples"), 1, evidence.MAX_TOTAL_POINTS)
        zeros = integer(value.get("zeroSamples"), 0, samples)
        total = integer(value.get("total"))
        peak = integer(value.get("observedPeakHourlyCount"))
        if (
            samples != series * snapshot.window.hours or value.get("expectedSamples") != samples
            or integer(value.get("observedTotal")) != total
            or not peak <= total <= peak * snapshot.window.hours
            or (zeros == samples) != (total == 0)
            or value.get("firstSampleAt") != evidence.utc_text(snapshot.window.start)
            or value.get("lastSampleAt") != evidence.utc_text(snapshot.window.end - timedelta(hours=1))
        ):
            raise evidence.EvidenceError("incomplete_usage_samples")
    value = values[pool.metric]
    if snapshot.window.hours < pool.minimum_hours or value["total"] < pool.minimum_total:
        raise evidence.EvidenceError("insufficient_usage_for_sizing")
    peak = value["observedPeakHourlyCount"]
    demand = (peak + pool.count_per_capacity_hour - 1) // pool.count_per_capacity_hour
    if demand > member.ceiling:
        raise evidence.EvidenceError("observed_demand_above_production_ceiling")
    return max(member.floor, demand)


def recommend(policy: Policy, snapshot: Snapshot, now: datetime) -> dict:
    output = []
    covered = set()
    for pool in policy.pools:
        members = policy.members(pool)
        covered.update(d.key for d in members)
        rows = [{
            "name": d.deployment.name, "region": d.deployment.region, "model": d.deployment.model.public(),
            "critical": d.critical, "floor": d.floor, "ceiling": d.ceiling,
            "currentCapacity": (
                snapshot.deployments[d.key]["live"]["capacity"]
                if snapshot.deployments[d.key]["inventoryStatus"] == "matched" else None
            ),
            "action": "hold", "recommendedCapacity": None,
        } for d in members]
        result = {
            "id": pool.id, "pool": pool.assertion, "authority": "operator_asserted",
            "status": "unknown", "codes": [], "evidenceCodes": [], "budget": None, "deployments": rows,
            "sizingBasis": {
                "authority": "operator_sizing_assumption", "metric": pool.metric,
                "countPerCapacityHour": pool.count_per_capacity_hour,
                "minimumHours": pool.minimum_hours, "minimumTotal": pool.minimum_total,
            },
        }
        try:
            matching = [p for p in snapshot.pools if {k: p[k] for k in POOL_FIELDS} == pool.assertion]
            if len(matching) != 1:
                raise evidence.EvidenceError("pool_assertion_not_established")
            observed = matching[0]
            result["evidenceCodes"] = observed["codes"]
            if observed["status"] != "consistent" or observed["codes"]:
                raise evidence.EvidenceError("incomplete_pool_evidence")
            if (observed["counter"], observed["scope"]) in snapshot.pool_mismatches:
                raise evidence.EvidenceError("pool_rollup_mismatch")
            expected = {
                (d.region, d.name) for d in policy.scope.catalog.deployments
                if d.region in pool.assertion["regions"]
                and (d.model.format, d.model.name, d.model.sku)
                == tuple(pool.assertion["model"][k] for k in ("format", "name", "sku"))
            }
            if {d.key for d in members} != expected:
                raise evidence.EvidenceError("production_deployment_not_configured")
            targets = {d.key: _usage_target(d, pool, snapshot) for d in members}
            budget_result = budget(
                pool, members, targets, current=observed["counterCurrentValue"],
                limit=observed["counterLimit"], catalog_allocation=observed["catalogAllocation"],
            )
            increase = check_increase_headroom(
                pool, targets, {d.key: snapshot.deployments[d.key]["live"]["capacity"] for d in members},
                observed["counterCurrentValue"], observed["counterLimit"],
            )
            if increase:
                # Platform numbers are not additive pools. A conservative bound is
                # still only an observation, never a promise of deployability.
                if increase > min(p["availableCapacity"] for p in observed["platformAvailability"]):
                    raise evidence.EvidenceError("platform_headroom_insufficient")
            for row in rows:
                target = targets[(row["region"], row["name"])]
                row["recommendedCapacity"] = target
                row["action"] = "increase" if target > row["currentCapacity"] else "decrease" if target < row["currentCapacity"] else "hold"
            result["status"] = "recommended"
            result["budget"] = budget_result
        except evidence.EvidenceError as exc:
            result["codes"] = [exc.code]
            for row in rows:
                row["recommendedCapacity"] = row["currentCapacity"]
        output.append(result)
    unmapped = [
        {"region": d.region, "name": d.name, "action": "hold", "status": "unknown"}
        for d in policy.scope.catalog.deployments if (d.region, d.name) not in covered
    ]
    return {
        "schemaVersion": 1, "version": REPORT_VERSION,
        "status": "complete" if not unmapped and all(p["status"] == "recommended" for p in output) else "partial",
        "generatedAt": evidence.utc_text(now), "scope": policy.scope.public(),
        "policy": {"id": policy.id, "version": VERSION},
        "source": {
            "reportSha256": snapshot.report_digest, "catalogSha256": policy.scope.catalog.digest,
            "poolEvidenceSha256": snapshot.pool_digest, "collectedAt": snapshot.finished_at,
        },
        "window": snapshot.window.public(), "pools": output, "unmappedDeployments": unmapped,
        "writes": "none", "azureCalls": 0,
        "limitations": [
            "Operator-asserted pool identity and sizing assumptions, not Azure-authoritative pool discovery.",
            "Returned aggregate series are bounded evidence, not durable sizing or proof of complete workload coverage.",
            "No removal, automatic adoption, catalog patch, environment update or deployment is performed.",
            "Review and a separate fresh provisioning preflight are required; pricing remains unknown.",
        ],
    }


def render(report: dict, output_format: str) -> str:
    serialized = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(serialized.encode("utf-8")) > evidence.MAX_REPORT_BYTES:
        raise evidence.EvidenceError("report_too_large")
    if output_format == "json":
        return serialized
    lines = [
        f"Production capacity recommendations: {report['status']} (offline; no writes)",
        f"Policy: {report['policy']['id']} / {report['policy']['version']}",
        f"Catalog SHA-256: {report['source']['catalogSha256']}",
        f"Report SHA-256: {report['source']['reportSha256']}",
    ]
    for pool in report["pools"]:
        assertion = pool["pool"]
        lines.append(
            f"Pool {pool['id']} [{assertion['counter']}, {assertion['scope']}, {assertion['unit']}; "
            f"operator_asserted]: {pool['status']} ({', '.join(pool['codes']) or 'review required'})"
        )
        for row in pool["deployments"]:
            lines.append(f"  {row['region']} / {row['name']}: {row['action']} {row['currentCapacity']} -> {row['recommendedCapacity']}")
        if pool["budget"] is not None:
            b = pool["budget"]
            lines.append(f"  Headroom after: {b['headroomAfter']}; reserved: {sum(b['reserve'].values())}; unreserved: {b['unreservedHeadroomAfter']}")
    for row in report["unmappedDeployments"]:
        lines.append(f"Unconfigured: {row['region']} / {row['name']}; unknown, hold")
    lines.extend(report["limitations"])
    return "\n".join(lines) + "\n"
