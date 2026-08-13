#!/usr/bin/env python3
"""Plan or record the maximum deployable capacity for existing catalog models.

The baseline ``capacity`` in ``infra/models.json`` stays portable. This script
queries the selected subscription's live deployments, quota counters, and
``modelCapacities`` API, then writes an optional ``maxCapacity`` beside each
existing deployment. Bicep selects that value only when
``AI4IA_MODEL_CAPACITY_PROFILE=maximum``.

Pool scope is inferred from Azure's own numbers rather than a publisher
allowlist:

* Global pools satisfy ``current across all regions + available == limit``.
* Regional pools satisfy ``current in region + available == limit``.
* Data-zone pools are allocated independently by ``models.regions[].dataZone``.

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
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
MODELS_FILE = ROOT / "infra" / "models.json"


def _az_executable() -> str:
    return "az.cmd" if os.name == "nt" else "az"


def _az_json(*args: str) -> Any:
    result = subprocess.run(
        [_az_executable(), *args, "-o", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Azure CLI failed: az {' '.join(args)}\n"
            f"{result.stderr.strip() or 'No error detail was returned.'}"
        )
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Azure CLI returned invalid JSON for az {' '.join(args)}: {exc.msg}"
        ) from exc


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _quota_keys(sku: str, model: str) -> list[str]:
    sku_key = _normal(sku)
    candidates = [model, model.replace(".0", ""), re.sub(r"\.azure$", "", model, flags=re.I)]
    return list(dict.fromkeys(f"{sku_key}|{_normal(candidate)}" for candidate in candidates))


def index_quota(raw: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in raw:
        counter = str((item.get("name") or {}).get("value") or "")
        if counter.count(".") < 2:
            continue
        _publisher, _, remainder = counter.partition(".")
        sku, _, model = remainder.partition(".")
        if not sku or not model:
            continue
        entry = {
            "counter": counter,
            "current": int(float(item.get("currentValue") or 0)),
            "limit": int(float(item.get("limit") or 0)),
        }
        for key in _quota_keys(sku, model):
            index.setdefault(key, entry)
    return index


def deployment_name(models: dict[str, Any], model: str, deployment: dict[str, Any]) -> str:
    naming = models["naming"]
    return str(naming.get("pattern") or "{model}-{subscriptionToken}-{region}-{skuShort}").format(
        model=model,
        subscriptionToken=naming["subscriptionToken"],
        region=deployment["region"],
        skuShort=naming["skuShort"][deployment["sku"]],
    )


def _platform_index(raw: dict[str, Any]) -> dict[tuple[str, str], int]:
    indexed: dict[tuple[str, str], int] = {}
    for item in raw.get("value") or []:
        properties = item.get("properties") or {}
        region = _normal(item.get("location"))
        sku = str(properties.get("skuName") or "")
        if region and sku:
            indexed[(region, sku.casefold())] = int(properties.get("availableCapacity") or 0)
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
    quotas = {
        region: index_quota(raw)
        for region, raw in quota_by_region.items()
    }
    regions = models["regions"]
    active: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    missing_non_anthropic: list[str] = []

    for model in models["catalog"]:
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
    models: dict[str, Any], resource_group: str, environment_name: str
) -> dict[str, dict[str, int]]:
    accounts = _az_json(
        "cognitiveservices", "account", "list", "--resource-group", resource_group
    )
    foundry_token = models["naming"]["foundryToken"]
    live: dict[str, dict[str, int]] = {}
    for region in models["regions"]:
        prefix = f"mf-{foundry_token}-{environment_name}-{region}-".casefold()
        matches = [
            account
            for account in accounts
            if str(account.get("kind") or "").casefold() == "aiservices"
            and _normal(account.get("location")) == _normal(region)
            and str(account.get("name") or "").casefold().startswith(prefix)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one Foundry account for {region} in {resource_group}; "
                f"found {len(matches)}."
            )
        deployments = _az_json(
            "cognitiveservices",
            "account",
            "deployment",
            "list",
            "--resource-group",
            resource_group,
            "--name",
            matches[0]["name"],
        )
        live[region] = {
            str(item["name"]): int((item.get("sku") or {}).get("capacity") or 0)
            for item in deployments
        }
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
    args = parser.parse_args()

    models = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    account = _az_json("account", "show")
    subscription_id = str(account.get("id") or "")
    if args.subscription and subscription_id.casefold() != args.subscription.casefold():
        raise SystemExit(
            f"Active subscription is {subscription_id}, expected {args.subscription}."
        )

    live = _collect_live(models, args.resource_group, args.environment_name)
    active_names = {name for deployments in live.values() for name in deployments}
    quota = {
        region: _az_json("cognitiveservices", "usage", "list", "--location", region)
        for region in models["regions"]
    }
    platform = _collect_platform(subscription_id, models, active_names)
    plan, pools, diagnostics = build_capacity_plan(models, live, quota, platform)

    baseline_total = 0
    maximum_total = 0
    active_baseline_total = 0
    live_total = 0
    active_maximum_total = 0
    changed_from_live = 0
    plan_rows: list[dict[str, Any]] = []
    for model in models["catalog"]:
        for deployment in model["deployments"]:
            key = (model["name"], deployment["region"], deployment["sku"])
            baseline = int(deployment["capacity"])
            maximum = int(plan.get(key, deployment.get("maxCapacity", baseline)))
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
        f"{changed_from_live} live increases, {live_total} -> "
        f"{active_maximum_total} active units (+{active_maximum_total - live_total})."
    )
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
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
