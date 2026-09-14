"""Synthetic policy only: no real criticality, capacity, quota or reserve decisions."""

from __future__ import annotations

import copy
import json
from pathlib import Path

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def production_document(
    sku: str = "GlobalStandard", scopes: tuple[str, ...] = ("global",),
    *, shared_zone: bool = False, include_cu: bool = False,
) -> dict:
    regions = {
        "eastus2": {"dataZone": "US", "primary": True},
        "swedencentral": {"dataZone": "US" if shared_zone else "EU", "primary": True},
    }
    pools = []
    deployments = []
    for index, region in enumerate(regions):
        deployments.append({
            "region": region, "sku": sku, "version": str(index + 1),
            "capacity": 10, "maxCapacity": 100, "maxCapacityPool": "global",
            "production": {
                "poolId": "", "critical": index == 0, "criticalMinimum": 30 if index == 0 else 0,
                "ceiling": 100, "capacity": 40 if index == 0 else 20,
            },
        })
    for index, scope in enumerate(scopes):
        membership = [
            region for region, config in regions.items()
            if scope == "global" or scope == f"region:{region}" or scope == f"data-zone:{config['dataZone']}"
        ]
        pool_id = f"pool-{index}"
        for deployment in deployments:
            if deployment["region"] in membership:
                deployment["production"]["poolId"] = pool_id
        pools.append({
            "id": pool_id,
            "pool": {
                "counter": f"AIServices.{sku}.model-a", "unit": "Count", "scope": scope,
                "regions": membership,
                "model": {
                    "format": "OpenAI", "name": "model-a", "sku": sku,
                    "versions": [d["version"] for d in deployments if d["region"] in membership],
                },
                "capacityUnitsPerCounterUnit": 1,
            },
            "reserve": {"replacement": 60, "retry": 10, "otherWorkloads": 20},
            "usage": {
                "metric": "ModelRequests", "countPerCapacityHour": 10,
                "minimumHours": 24, "minimumTotal": 1000,
            },
        })
    document = {
        "_comment": "SYNTHETIC TEST POLICY. Not an approved production assignment.",
        "naming": {
            "foundryToken": "demo", "subscriptionToken": "example",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {"GlobalStandard": "glbl", "DataZoneStandard": "dz", "Standard": "std"},
        },
        "regions": regions,
        "productionCapacityPolicy": {
            "version": "production-capacity-v1", "id": "synthetic-policy-v1",
            "subscriptionId": SUBSCRIPTION, "resourceGroup": "rg-demo-example", "environment": "example",
            "review": {
                "reference": "synthetic-review", "reviewedAt": "2026-09-09T10:00:00Z",
                "catalogSha256": "a" * 64, "reportSha256": "b" * 64, "poolEvidenceSha256": "c" * 64,
            },
            "pools": pools,
        },
        "catalog": [{"name": "model-a", "format": "OpenAI", "category": "chat", "deployments": deployments}],
    }
    if include_cu:
        # The full application gate also requires its existing primary CU models.
        # Copy their pins from the source catalog into this synthetic stack only.
        source = json.loads((Path(__file__).resolve().parents[2] / "infra" / "models.json").read_text(encoding="utf-8"))
        for name in ("gpt-5.2", "text-embedding-3-large"):
            model = copy.deepcopy(next(m for m in source["catalog"] if m["name"] == name))
            deployment = copy.deepcopy(next(d for d in model["deployments"] if d["region"] == "eastus2" and d["sku"] == "GlobalStandard"))
            pool_id = f"fixture-{name}"
            deployment["production"] = {
                "poolId": pool_id, "critical": False, "criticalMinimum": 0,
                "ceiling": deployment["capacity"], "capacity": deployment["capacity"],
            }
            model["deployments"] = [deployment]
            document["catalog"].append(model)
            pool = copy.deepcopy(pools[0])
            pool.update(id=pool_id)
            pool["reserve"]["replacement"] = deployment["capacity"]
            pool["pool"].update({
                "counter": f"OpenAI.GlobalStandard.{name}", "scope": "region:eastus2", "regions": ["eastus2"],
                "model": {"format": model["format"], "name": name, "sku": deployment["sku"], "versions": [deployment["version"]]},
            })
            document["productionCapacityPolicy"]["pools"].append(pool)
    return document
