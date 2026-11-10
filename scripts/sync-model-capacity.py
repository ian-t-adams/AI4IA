#!/usr/bin/env python3
"""Plan or record the maximum deployable capacity for existing catalog models.

The baseline ``capacity`` in ``infra/models.json`` stays portable. This script
queries the selected subscription's live deployments, quota counters, and
``modelCapacities`` API, then writes an optional ``maxCapacity`` beside each
existing deployment. Bicep selects that value only when
``AI4IA_MODEL_CAPACITY_PROFILE=maximum``.

Legacy pool selection uses the existing numerical rules below. They do not prove
pool ownership or outside/all-version membership. Planning refuses contradictory
or nonzero unattributed usage, multiple versions sharing one counter, and
missing/malformed/continued quota or platform evidence instead of guessing:

* Global pools satisfy ``current across all regions + available == limit``.
* Regional pools satisfy ``current in region + available == limit``.
* Data-zone pools are allocated independently by ``models.regions[].dataZone``.

``--reduce-to-quota`` repairs recorded pools after a quota decrease without
increasing any deployment, changing pool membership, or going below a baseline.
Even an apparently healthy recorded pool must pass the same evidence checks.
The command is read-only unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _model_naming import deployment_name
from _model_targets import model_target, provisioning_enabled, runtime_enabled, source_catalog

ROOT = Path(__file__).resolve().parents[1]
MODELS_FILE = ROOT / "infra" / "models.json"


def _az_executable() -> str:
    return "az.cmd" if os.name == "nt" else "az"


def _az_json(*args: str) -> Any:
    operation = next((label for prefix, label in (
        (("account", "show"), "account show"),
        (("cognitiveservices", "account", "deployment", "list"), "deployment inventory"),
        (("cognitiveservices", "account", "list"), "account inventory"),
        (("cognitiveservices", "usage", "list"), "quota usage"),
        (("rest", "--method", "GET"), "platform capacity read"),
    ) if args[:len(prefix)] == prefix), "unrecognized read")
    if "--location" in args:
        index = args.index("--location") + 1
        region = args[index] if index < len(args) else ""
        operation += f" in {region}" if re.fullmatch(r"[a-z][a-z0-9]{0,62}", region, flags=re.ASCII) else " in unknown region"
    try:
        result = subprocess.run(
            [_az_executable(), *args, "-o", "json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        raise RuntimeError(f"Azure CLI {operation} failed (OSError); untrusted error detail withheld.") from None
    if result.returncode != 0:
        raise RuntimeError(
            f"Azure CLI {operation} failed (nonzero exit status); untrusted error detail withheld."
        )
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Azure CLI {operation} returned invalid JSON: {exc.msg}"
        ) from exc


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _quota_keys(sku: str, model: str) -> list[str]:
    sku_key = _normal(sku)
    candidates = [model, model.replace(".0", ""), re.sub(r"\.azure$", "", model, flags=re.IGNORECASE)]
    return list(dict.fromkeys(f"{sku_key}|{_normal(candidate)}" for candidate in candidates))


def index_quota(raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise TypeError("Complete quota inventory is required.")
    index: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), dict):
            raise TypeError("Invalid quota counter identity.")
        counter = item["name"].get("value")
        if not isinstance(counter, str) or not counter or counter != counter.strip():
            raise ValueError("Invalid quota counter identity.")
        if counter.count(".") < 2:
            continue
        _publisher, _, remainder = counter.partition(".")
        sku, _, model = remainder.partition(".")
        if not sku or not model:
            continue
        if any(type(item.get(field)) is not int or item[field] < 0 for field in ("currentValue", "limit")):
            raise ValueError(f"{counter}: quota currentValue and limit must be known nonnegative integers.")
        entry = {
            "counter": counter,
            "current": item["currentValue"],
            "limit": item["limit"],
        }
        for key in _quota_keys(sku, model):
            if key in index:
                raise ValueError(f"{counter}: duplicate or aliased quota counter evidence.")
            index[key] = entry
    return index


def _platform_index(raw: dict[str, Any]) -> dict[tuple[str, str], int]:
    if not isinstance(raw, dict) or set(raw) - {"value"} or not isinstance(raw.get("value"), list):
        raise ValueError("Complete platform capacity inventory is required.")
    indexed: dict[tuple[str, str], int] = {}
    for item in raw["value"]:
        if not isinstance(item, dict) or not isinstance(item.get("properties"), dict):
            raise TypeError("Invalid platform capacity evidence.")
        properties = item["properties"]
        location, sku = item.get("location"), properties.get("skuName")
        available = properties.get("availableCapacity")
        if (
            not isinstance(location, str) or not location.strip()
            or not isinstance(sku, str) or not sku.strip() or sku != sku.strip()
            or type(available) is not int or available < 0
        ):
            raise ValueError("Platform identity and nonnegative integer capacity must be explicit.")
        key = _normal(location), sku.casefold()
        if key in indexed:
            raise ValueError("Duplicate platform capacity identity.")
        indexed[key] = available
    return indexed


def _balanced_allocate(
    entries: list[dict[str, Any]], target_total: int
) -> dict[tuple[str, str, str], int]:
    """Raise the lowest-capacity deployments first without exceeding local availability."""
    capacities = [int(entry["current"]) for entry in entries]
    ceilings = [
        int(entry["current"]) + int(entry["available"])
        for entry in entries
    ]
    remaining = target_total - sum(capacities)
    heap = [
        (capacities[index], str(entry["region"]), index)
        for index, entry in enumerate(entries)
        if capacities[index] < ceilings[index]
    ]
    heapq.heapify(heap)
    while remaining > 0 and heap:
        capacity, region, index = heapq.heappop(heap)
        if capacity != capacities[index]:
            continue
        capacities[index] += 1
        remaining -= 1
        if capacities[index] < ceilings[index]:
            heapq.heappush(heap, (capacities[index], region, index))
    return {
        (entry["model"], entry["region"], entry["sku"]): capacities[index]
        for index, entry in enumerate(entries)
    }


def planning_catalog(models: dict[str, Any], live: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Only complete absence may omit staged rows; unmanaged retained planning refuses."""
    catalog = []
    for model in models["catalog"]:
        provisioning, runtime = provisioning_enabled(model), runtime_enabled(model)
        if model_target(model) != "source":
            catalog.append(model)
            continue
        deployments = []
        for deployment in model["deployments"]:
            region = deployment["region"]
            if region not in live or not isinstance(live[region], dict):
                raise ValueError(f"{region}: complete deployment inventory is unavailable.")
            name = deployment_name(models, model["name"], deployment)
            present = any(key.casefold() == name.casefold() for key in live[region])
            if present and not provisioning:
                raise ValueError(f"{name}: retained provisioning-disabled capacity planning is unsupported; no changes recorded.")
            if name not in live[region]:
                if provisioning or runtime:
                    raise ValueError(f"{name}: required externally managed or provisioned deployment is absent.")
                continue
            if type(live[region][name]) is not int or live[region][name] <= 0:
                raise ValueError(f"{name}: live allocation must be a positive integer.")
            deployments.append(deployment)
        catalog.append({**model, "deployments": deployments})
    return {**models, "catalog": catalog}


def _validate_counter_versions(
    models: dict[str, Any], quotas: dict[str, dict[str, dict[str, Any]]],
) -> None:
    members: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
    for model in models["catalog"]:
        if model_target(model) != "source":
            continue
        for deployment in model["deployments"]:
            for key in _quota_keys(deployment["sku"], model["name"]):
                counter = quotas.get(deployment["region"], {}).get(key)
                if counter is not None:
                    _publisher, counter_sku, counter_model = counter["counter"].split(".", 2)
                    for alias in _quota_keys(counter_sku, counter_model):
                        members[alias].add((
                            model["format"], model["name"], deployment["version"], deployment["sku"],
                        ))
                    break
    if any(len(identities) != 1 for identities in members.values()):
        raise ValueError("Multiple model/version identities share a quota counter; planning is unsupported.")


def reduce_capacity_to_quota(
    models: dict[str, Any],
    live_by_region: dict[str, dict[str, int]],
    quota_by_region: dict[str, list[dict[str, Any]]],
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], str], list[str]]:
    """Reduce only over-limit recorded pools; retain outside allocation and baselines."""
    models = planning_catalog(models, live_by_region)
    quotas = {region: index_quota(rows) for region, rows in quota_by_region.items()}
    _validate_counter_versions(models, quotas)
    plan: dict[tuple[str, str, str], int] = {}
    pools: dict[tuple[str, str, str], str] = {}
    diagnostics: list[str] = []
    for model in models["catalog"]:
        if model_target(model) != "source":
            continue
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for deployment in model["deployments"]:
            pool = deployment.get("maxCapacityPool")
            if pool is not None:
                grouped[(deployment["sku"], pool)].append(deployment)
        for (sku, pool), deployments in grouped.items():
            observed = []
            for deployment in deployments:
                region = deployment["region"]
                counter = next((
                    quotas.get(region, {})[key]
                    for key in _quota_keys(sku, model["name"])
                    if key in quotas.get(region, {})
                ), None)
                if counter is None:
                    raise ValueError(f"{model['name']} ({sku}, {pool}): quota evidence is missing.")
                observed.append(counter)
            if len({(row["counter"].casefold(), row["limit"], row["current"]) for row in observed}) != 1:
                raise ValueError(f"{model['name']} ({sku}, {pool}): quota replicas disagree.")
            counter = observed[0]
            recorded = [int(deployment.get("maxCapacity", deployment["capacity"])) for deployment in deployments]
            if pool != "global" and pool not in {
                f"region:{deployment['region']}" for deployment in deployments
            } | {
                f"data-zone:{models['regions'][deployment['region']]['dataZone']}"
                for deployment in deployments
            }:
                raise ValueError(f"{model['name']} ({sku}): unrecognized recorded pool {pool!r}.")
            current = []
            for deployment in deployments:
                name = deployment_name(models, model["name"], deployment)
                value = live_by_region.get(deployment["region"], {}).get(name)
                if type(value) is not int or value < 0:
                    raise ValueError(f"{name}: live allocation is unavailable.")
                current.append(value)
            if counter["current"] < sum(current):
                raise ValueError(f"{model['name']} ({sku}, {pool}): quota usage contradicts live allocation.")
            if counter["current"] != sum(current):
                raise ValueError(
                    f"{model['name']} ({sku}, {pool}): outside allocation or shared-counter membership "
                    "is unproven; no changes recorded."
                )
            outside = 0
            if sum(recorded) <= counter["limit"]:
                continue
            target = counter["limit"] - outside
            baselines = [int(deployment["capacity"]) for deployment in deployments]
            if target < sum(baselines):
                raise ValueError(f"{model['name']} ({sku}, {pool}): quota cannot fit the portable baselines.")
            members = [
                {
                    "model": model["name"], "region": deployment["region"], "sku": sku,
                    "current": baseline, "available": min(recorded[index], current[index]) - baseline,
                }
                for index, (deployment, baseline) in enumerate(zip(deployments, baselines, strict=True))
            ]
            if any(member["available"] < 0 for member in members):
                raise ValueError(f"{model['name']} ({sku}, {pool}): live allocation is below baseline.")
            allocated = _balanced_allocate(members, target)
            if sum(allocated.values()) != target:
                raise ValueError(f"{model['name']} ({sku}, {pool}): reduction would require an increase.")
            plan.update(allocated)
            pools.update({key: pool for key in allocated})
            diagnostics.append(
                f"{model['name']} ({sku}, {pool}): recorded {sum(recorded)} -> {target} units; "
                f"outside allocation {outside}, quota {counter['limit']} [{counter['counter']}]"
            )
    return plan, pools, diagnostics


def build_capacity_plan(
    models: dict[str, Any],
    live_by_region: dict[str, dict[str, int]],
    quota_by_region: dict[str, list[dict[str, Any]]],
    platform_by_model: dict[tuple[str, str, str], dict[str, Any]],
) -> tuple[
    dict[tuple[str, str, str], int],
    dict[tuple[str, str, str], str],
    list[str],
]:
    """Return maximum capacities for live deployments plus diagnostics."""
    models = planning_catalog(models, live_by_region)
    quotas = {
        region: index_quota(raw)
        for region, raw in quota_by_region.items()
    }
    _validate_counter_versions(models, quotas)
    regions = models["regions"]
    active: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    missing_non_anthropic: list[str] = []

    for model in models["catalog"]:
        if model_target(model) != "source":
            diagnostics.append(f"{model['name']}: external target is unavailable to the source capacity planner.")
            continue
        for deployment in model["deployments"]:
            region = deployment["region"]
            name = deployment_name(models, model["name"], deployment)
            actual = live_by_region.get(region, {}).get(name)
            if actual is None:
                if model.get("format") != "Anthropic":
                    missing_non_anthropic.append(name)
                continue
            baseline = int(deployment["capacity"])
            if actual < baseline:
                raise ValueError(
                    f"{name}: live capacity is {actual}, below the models.json baseline "
                    f"{baseline}; reconcile drift before generating a maximum profile."
                )
            quota_entry = next(
                (
                    quotas[region][key]
                    for key in _quota_keys(deployment["sku"], model["name"])
                    if key in quotas[region]
                ),
                None,
            )
            if quota_entry is None:
                raise ValueError(
                    f"{name}: no quota counter matched {model['name']} / {deployment['sku']}."
                )
            platform = _platform_index(
                platform_by_model[(model["format"], model["name"], deployment["version"])]
            )
            available = platform.get((_normal(region), deployment["sku"].casefold()))
            if available is None:
                raise ValueError(
                    f"{name}: modelCapacities returned no {deployment['sku']} record in {region}."
                )
            active.append(
                {
                    "model": model["name"],
                    "format": model["format"],
                    "version": deployment["version"],
                    "region": region,
                    "dataZone": regions[region]["dataZone"],
                    "sku": deployment["sku"],
                    "current": actual,
                    "available": available,
                    "limit": int(quota_entry["limit"]),
                    "quotaCurrent": int(quota_entry["current"]),
                    "counter": quota_entry["counter"],
                    "publisher": quota_entry["counter"].partition(".")[0],
                }
            )

    if missing_non_anthropic:
        raise ValueError(
            "Catalog deployments are absent from the target environment: "
            + ", ".join(sorted(missing_non_anthropic))
        )

    by_model_sku: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in active:
        by_model_sku[
            (entry["format"], entry["model"], entry["version"], entry["sku"])
        ].append(entry)

    plan: dict[tuple[str, str, str], int] = {}
    pools: dict[tuple[str, str, str], str] = {}
    for (_format, model, _version, sku), entries in sorted(by_model_sku.items()):
        if sku == "DataZoneStandard":
            scoped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for entry in entries:
                scoped[entry["dataZone"]].append(entry)
        elif sku == "Standard":
            scoped = {entry["region"]: [entry] for entry in entries}
        elif any(
            str(entry["publisher"]).casefold() == "aiservices"
            for entry in entries
        ):
            scoped = {"global": entries}
        else:
            total = sum(int(entry["current"]) for entry in entries)
            global_matches = all(
                int(entry["available"]) + total == int(entry["limit"])
                for entry in entries
            )
            regional_matches = all(
                int(entry["available"]) + int(entry["current"]) == int(entry["limit"])
                for entry in entries
            )
            if global_matches or len(entries) == 1:
                scoped = {"global": entries}
            elif regional_matches:
                scoped = {entry["region"]: [entry] for entry in entries}
            elif all(int(entry["available"]) == 0 for entry in entries):
                scoped = {entry["region"]: [entry] for entry in entries}
                diagnostics.append(
                    f"{model} ({sku}) has no additional platform capacity; "
                    "retaining its existing regional allocations."
                )
            else:
                observed = ", ".join(
                    f"{entry['region']}: current={entry['current']} "
                    f"available={entry['available']} limit={entry['limit']}"
                    for entry in entries
                )
                raise ValueError(
                    f"{model} ({sku}) pool scope is ambiguous: {observed}"
                )

        for scope, members in scoped.items():
            limits = {int(entry["limit"]) for entry in members}
            if len(limits) != 1:
                raise ValueError(
                    f"{model} ({sku}, {scope}) reports inconsistent quota limits: "
                    f"{sorted(limits)}"
                )
            limit = limits.pop()
            current_total = sum(int(entry["current"]) for entry in members)
            counters = {(entry["counter"].casefold(), entry["quotaCurrent"]) for entry in members}
            if len(counters) != 1:
                raise ValueError(f"{model} ({sku}, {scope}): quota replicas disagree.")
            if next(iter(counters))[1] != current_total:
                raise ValueError(
                    f"{model} ({sku}, {scope}): outside allocation or shared-counter membership "
                    "is unproven or contradictory; no changes recorded."
                )
            if current_total > limit:
                raise ValueError(f"{model} ({sku}, {scope}): live allocation exceeds quota; maximum planning refuses.")
            target_total = min(
                limit,
                current_total
                + max(int(entry["available"]) for entry in members),
            )
            target_total = max(current_total, target_total)
            allocated = _balanced_allocate(members, target_total)
            plan.update(allocated)
            pool_name = (
                f"data-zone:{scope}"
                if sku == "DataZoneStandard"
                else f"region:{scope}"
                if scope != "global"
                else "global"
            )
            for key in allocated:
                pools[key] = pool_name
            diagnostics.append(
                f"{model} ({sku}, {scope}): {current_total} -> {target_total} "
                f"units [{members[0]['counter']}]"
            )

    return plan, pools, diagnostics


def _collect_live(
    models: dict[str, Any], resource_group: str, environment_name: str, subscription_id: str
) -> dict[str, dict[str, int]]:
    for model in models["catalog"]:
        provisioning_enabled(model)
        runtime_enabled(model)
    accounts = _az_json(
        "cognitiveservices", "account", "list", "--resource-group", resource_group,
        "--subscription", subscription_id,
    )
    if not isinstance(accounts, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip()
        for row in accounts
    ):
        raise ValueError("Complete, valid account inventory is required.")
    foundry_token = models["naming"]["foundryToken"]
    live: dict[str, dict[str, int]] = {}
    for region in models["regions"]:
        prefix = f"mf-{foundry_token}-{environment_name}-{region}-".casefold()
        matches = [
            account
            for account in accounts
            if account["name"].casefold().startswith(prefix)
        ]
        if not matches and not any(
            provisioning_enabled(model) or runtime_enabled(model)
            for model in models["catalog"] for d in model["deployments"] if d["region"] == region
        ):
            live[region] = {}
            continue
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one Foundry account for {region} in {resource_group}; "
                f"found {len(matches)}."
            )
        if (
            not isinstance(matches[0].get("kind"), str) or matches[0]["kind"].casefold() != "aiservices"
            or not isinstance(matches[0].get("location"), str)
            or re.sub(r"\s+", "", matches[0]["location"].casefold()) != region
        ):
            raise ValueError("Candidate account kind/location is invalid.")
        deployments = _az_json(
            "cognitiveservices",
            "account",
            "deployment",
            "list",
            "--resource-group",
            resource_group,
            "--name",
            matches[0]["name"],
            "--subscription", subscription_id,
        )
        if not isinstance(deployments, list):
            raise ValueError("Complete deployment inventory is required.")
        observed: dict[str, int] = {}
        for item in deployments:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValueError("Invalid deployment identity.")
            name = item["name"]
            if name.casefold() in {key.casefold() for key in observed}:
                raise ValueError("Duplicate deployment identity.")
            # Refuse before projecting away identity/type evidence, even for malformed retained rows.
            if any(
                not provisioning_enabled(model) and deployment_name(models, model["name"], d).casefold() == name.casefold()
                for model in models["catalog"] for d in model["deployments"] if d["region"] == region
            ):
                raise ValueError("Retained provisioning-disabled deployment planning is unsupported.")
            expected = [
                (model, d) for model in models["catalog"] for d in model["deployments"]
                if d["region"] == region and deployment_name(models, model["name"], d) == name
            ]
            if len(expected) != 1:
                raise ValueError("Unknown or ambiguous deployment allocation; planning is unsupported.")
            model, deployment = expected[0]
            expected_id = (
                f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/"
                f"Microsoft.CognitiveServices/accounts/{matches[0]['name']}/deployments/{name}"
            )
            properties = item.get("properties")
            native_model = properties.get("model") if isinstance(properties, dict) else None
            resource_id = item.get("id")
            if (
                not isinstance(resource_id, str) or resource_id.casefold() != expected_id.casefold()
                or not isinstance(properties, dict)
                or not isinstance(native_model, dict)
                or any(native_model.get(field) != value for field, value in (
                    ("format", model["format"]), ("name", model["name"]), ("version", deployment["version"]),
                ))
                or properties.get("provisioningState") != "Succeeded"
                or not isinstance(item.get("sku"), dict) or item["sku"].get("name") != deployment["sku"]
            ):
                raise ValueError("Deployment identity/model/SKU/state does not prove its quota allocation.")
            sku = item.get("sku")
            value = sku.get("capacity") if isinstance(sku, dict) else None
            if type(value) is not int or value <= 0:
                raise ValueError("Live deployment capacity must be a positive integer.")
            observed[name] = value
        live[region] = observed
    return live


def _collect_platform(
    subscription_id: str, models: dict[str, Any], active_names: set[str]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    queries = {
        (model["format"], model["name"], deployment["version"])
        for model in models["catalog"]
        for deployment in model["deployments"]
        if deployment_name(models, model["name"], deployment) in active_names
    }
    collected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for model_format, model_name, version in sorted(queries):
        collected[(model_format, model_name, version)] = _az_json(
            "rest",
            "--method",
            "GET",
            "--url",
            (
                "https://management.azure.com/subscriptions/"
                f"{subscription_id}/providers/Microsoft.CognitiveServices/modelCapacities"
            ),
            "--url-parameters",
            "api-version=2024-10-01",
            f"modelFormat={model_format}",
            f"modelName={model_name}",
            f"modelVersion={version}",
            "--subscription", subscription_id,
        )
    return collected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--environment-name", required=True)
    parser.add_argument("--subscription", help="Expected active subscription id.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write maxCapacity values to infra/models.json. Default is plan-only.",
    )
    parser.add_argument("--output-plan", type=Path)
    parser.add_argument(
        "--reduce-to-quota", action="store_true",
        help="Reduce only over-limit recorded pools; never increase live or desired capacities.",
    )
    args = parser.parse_args()

    models = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    for model in models["catalog"]:
        provisioning_enabled(model)
        runtime_enabled(model)
    external = [model for model in models["catalog"] if model_target(model) != "source"]
    if external and args.apply and os.environ.get("AI4IA_CLAUDE_ENABLED", "").lower() == "true":
        raise SystemExit(
            "External Claude capacity cannot be approved by this single-subscription planner; "
            "--apply is refused. Use the explicit target provisioning/readback contract."
        )
    account = _az_json("account", "show")
    subscription_id = str(account.get("id") or "")
    if args.subscription and subscription_id.casefold() != args.subscription.casefold():
        raise SystemExit(
            f"Active subscription is {subscription_id}, expected {args.subscription}."
        )

    source = source_catalog(models)
    live = _collect_live(source, args.resource_group, args.environment_name, subscription_id)
    planned_models = planning_catalog(models, live)
    active_names = {name for deployments in live.values() for name in deployments}
    quota = {
        region: _az_json(
            "cognitiveservices", "usage", "list", "--location", region,
            "--subscription", subscription_id,
        )
        for region in models["regions"]
    }
    if args.reduce_to_quota:
        plan, pools, diagnostics = reduce_capacity_to_quota(planned_models, live, quota)
    else:
        platform = _collect_platform(subscription_id, source, active_names)
        plan, pools, diagnostics = build_capacity_plan(planned_models, live, quota, platform)

    baseline_total = 0
    maximum_total = 0
    active_baseline_total = 0
    live_total = 0
    active_maximum_total = 0
    changed_from_live = 0
    increased = 0
    reduced = 0
    plan_rows: list[dict[str, Any]] = []
    for model in models["catalog"]:
        for deployment in model["deployments"]:
            key = (model["name"], deployment["region"], deployment["sku"])
            baseline = int(deployment["capacity"])
            recorded = deployment.get("maxCapacity", baseline)
            if type(recorded) is not int or recorded < baseline:
                raise ValueError(f"{model['name']}: maximum capacity must be an integer at or above baseline.")
            maximum = plan.get(key, recorded)
            baseline_total += baseline
            maximum_total += maximum
            if key in plan:
                name = deployment_name(models, model["name"], deployment)
                current = int(live[deployment["region"]][name])
                active_baseline_total += baseline
                live_total += current
                active_maximum_total += maximum
                if maximum != current:
                    changed_from_live += 1
                increased += maximum > current
                reduced += maximum < current
                deployment["maxCapacity"] = maximum
                deployment["maxCapacityPool"] = pools[key]
                plan_rows.append(
                    {
                        "model": model["name"],
                        "region": deployment["region"],
                        "sku": deployment["sku"],
                        "capacity": baseline,
                        "liveCapacity": current,
                        "maxCapacity": maximum,
                    }
                )

    print(
        f"Capacity plan: {len(plan_rows)} live deployments, "
        f"{increased} increases and {reduced} reductions, {live_total} -> "
        f"{active_maximum_total} active units ({active_maximum_total - live_total:+d})."
    )
    if args.reduce_to_quota:
        for diagnostic in diagnostics:
            print(f"  {diagnostic}")
    for row in sorted(
        plan_rows,
        key=lambda item: item["maxCapacity"] - item["liveCapacity"],
        reverse=True,
    )[:25]:
        delta = row["maxCapacity"] - row["liveCapacity"]
        if delta > 0:
            print(
                f"  {row['model']} {row['region']} {row['sku']}: "
                f"{row['liveCapacity']} -> {row['maxCapacity']} (+{delta})"
            )
    if changed_from_live > 25:
        print(f"  ... {changed_from_live - 25} additional live increases")

    if args.output_plan:
        args.output_plan.parent.mkdir(parents=True, exist_ok=True)
        args.output_plan.write_text(
            json.dumps(
                {
                    "summary": {
                        "liveDeployments": len(plan_rows),
                        "changedDeployments": changed_from_live,
                        "baselineCapacity": baseline_total,
                        "activeBaselineCapacity": active_baseline_total,
                        "liveCapacity": live_total,
                        "activeMaximumCapacity": active_maximum_total,
                        "maximumCapacity": maximum_total,
                    },
                    "deployments": plan_rows,
                    "pools": diagnostics,
                    "externalCoverage": {
                        "status": "unknown" if external else "not_applicable",
                        "deployments": [
                            {
                                "name": deployment_name(models, model["name"], deployment),
                                "target": model["deploymentTarget"],
                                "capacity": deployment["capacity"], "maximumCapacity": None,
                            }
                            for model in external for deployment in model["deployments"]
                        ],
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Wrote plan to {args.output_plan}")

    if args.apply:
        MODELS_FILE.write_text(json.dumps(models, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {MODELS_FILE.relative_to(ROOT)}.")
    else:
        print("Plan only; re-run with --apply to record maxCapacity values.")
    if external:
        print("External Claude coverage is unknown; no external maximum or headroom was inferred.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
