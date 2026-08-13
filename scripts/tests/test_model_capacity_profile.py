"""Capacity-profile allocation and IaC wiring contracts."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync-model-capacity.py"
PREFLIGHT = ROOT / "scripts" / "check-model-availability.py"

spec = importlib.util.spec_from_file_location("sync_model_capacity", SCRIPT)
assert spec and spec.loader
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)
preflight_spec = importlib.util.spec_from_file_location(
    "check_model_availability_capacity_profile", PREFLIGHT
)
assert preflight_spec and preflight_spec.loader
preflight = importlib.util.module_from_spec(preflight_spec)
preflight_spec.loader.exec_module(preflight)


def _models(sku: str = "GlobalStandard") -> dict:
    return {
        "naming": {
            "subscriptionToken": "sub",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {
                "GlobalStandard": "glbl",
                "DataZoneStandard": "dz",
                "Standard": "std",
            },
        },
        "regions": {
            "eastus2": {"dataZone": "US", "primary": True},
            "swedencentral": {"dataZone": "EU", "primary": True},
        },
        "catalog": [
            {
                "name": "model-a",
                "format": "OpenAI",
                "category": "chat",
                "deployments": [
                    {"region": "eastus2", "sku": sku, "capacity": 50, "version": "1"},
                    {
                        "region": "swedencentral",
                        "sku": sku,
                        "capacity": 50,
                        "version": "1",
                    },
                ],
            }
        ],
    }


def _live(sku: str, east: int = 50, sweden: int = 50) -> dict:
    short = {
        "GlobalStandard": "glbl",
        "DataZoneStandard": "dz",
        "Standard": "std",
    }[sku]
    return {
        "eastus2": {f"model-a-sub-eastus2-{short}": east},
        "swedencentral": {f"model-a-sub-swedencentral-{short}": sweden},
    }


def _quota(
    sku: str, current: int, limit: int, publisher: str = "OpenAI"
) -> dict:
    item = {
        "name": {"value": f"{publisher}.{sku}.model-a"},
        "currentValue": current,
        "limit": limit,
    }
    return {"eastus2": [item], "swedencentral": [item]}


def _platform(sku: str, east: int, sweden: int) -> dict:
    return {
        ("OpenAI", "model-a", "1"): {
            "value": [
                {
                    "location": "eastus2",
                    "properties": {"skuName": sku, "availableCapacity": east},
                },
                {
                    "location": "swedencentral",
                    "properties": {"skuName": sku, "availableCapacity": sweden},
                },
            ]
        }
    }


class CapacityAllocationTests(unittest.TestCase):
    def test_shared_global_pool_is_balanced_without_double_counting(self) -> None:
        plan, pools, _ = capacity.build_capacity_plan(
            _models(),
            _live("GlobalStandard"),
            _quota("GlobalStandard", current=100, limit=1000),
            _platform("GlobalStandard", east=900, sweden=900),
        )
        self.assertEqual(plan[("model-a", "eastus2", "GlobalStandard")], 500)
        self.assertEqual(plan[("model-a", "swedencentral", "GlobalStandard")], 500)
        self.assertEqual(pools[("model-a", "eastus2", "GlobalStandard")], "global")

    def test_region_scoped_global_pool_uses_each_region_limit(self) -> None:
        plan, pools, _ = capacity.build_capacity_plan(
            _models(),
            _live("GlobalStandard"),
            _quota("GlobalStandard", current=50, limit=300),
            _platform("GlobalStandard", east=250, sweden=250),
        )
        self.assertEqual(plan[("model-a", "eastus2", "GlobalStandard")], 300)
        self.assertEqual(plan[("model-a", "swedencentral", "GlobalStandard")], 300)
        self.assertEqual(
            pools[("model-a", "eastus2", "GlobalStandard")], "region:eastus2"
        )

    def test_data_zone_pools_are_independent(self) -> None:
        plan, pools, _ = capacity.build_capacity_plan(
            _models("DataZoneStandard"),
            _live("DataZoneStandard"),
            _quota("DataZoneStandard", current=50, limit=333),
            _platform("DataZoneStandard", east=283, sweden=283),
        )
        self.assertEqual(plan[("model-a", "eastus2", "DataZoneStandard")], 333)
        self.assertEqual(
            plan[("model-a", "swedencentral", "DataZoneStandard")], 333
        )
        self.assertEqual(
            pools[("model-a", "swedencentral", "DataZoneStandard")],
            "data-zone:EU",
        )

    def test_partner_global_pool_is_shared_even_when_counters_look_regional(self) -> None:
        plan, pools, _ = capacity.build_capacity_plan(
            _models(),
            _live("GlobalStandard"),
            _quota(
                "GlobalStandard",
                current=50,
                limit=300,
                publisher="AIServices",
            ),
            _platform("GlobalStandard", east=250, sweden=250),
        )
        self.assertEqual(plan[("model-a", "eastus2", "GlobalStandard")], 150)
        self.assertEqual(plan[("model-a", "swedencentral", "GlobalStandard")], 150)
        self.assertEqual(pools[("model-a", "eastus2", "GlobalStandard")], "global")

    def test_shared_data_zone_availability_is_counted_once(self) -> None:
        models = _models("DataZoneStandard")
        models["regions"]["swedencentral"]["dataZone"] = "US"
        plan, _, _ = capacity.build_capacity_plan(
            models,
            _live("DataZoneStandard"),
            _quota("DataZoneStandard", current=100, limit=1000),
            _platform("DataZoneStandard", east=300, sweden=300),
        )
        self.assertEqual(
            plan[("model-a", "eastus2", "DataZoneStandard")]
            + plan[("model-a", "swedencentral", "DataZoneStandard")],
            400,
        )

    def test_grandfathered_full_pool_is_never_reduced(self) -> None:
        models = _models()
        for deployment in models["catalog"][0]["deployments"]:
            deployment["capacity"] = 9
        plan, _, _ = capacity.build_capacity_plan(
            models,
            _live("GlobalStandard", east=9, sweden=9),
            _quota("GlobalStandard", current=9, limit=9),
            _platform("GlobalStandard", east=0, sweden=0),
        )
        self.assertEqual(plan[("model-a", "eastus2", "GlobalStandard")], 9)
        self.assertEqual(plan[("model-a", "swedencentral", "GlobalStandard")], 9)

    def test_live_capacity_below_baseline_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "below the models.json baseline"):
            capacity.build_capacity_plan(
                _models(),
                _live("GlobalStandard", east=49, sweden=50),
                _quota("GlobalStandard", current=99, limit=1000),
                _platform("GlobalStandard", east=901, sweden=901),
            )


class CapacityIacTests(unittest.TestCase):
    def test_bicep_selects_maximum_profile_explicitly(self) -> None:
        main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        parameters = json.loads(
            (ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8")
        )
        self.assertIn("param modelCapacityProfile string = 'baseline'", main)
        self.assertIn("d.?maxCapacity ?? d.capacity", main)
        self.assertEqual(
            parameters["parameters"]["modelCapacityProfile"]["value"],
            "${AI4IA_MODEL_CAPACITY_PROFILE=baseline}",
        )

    def test_preflight_selects_maximum_capacity_and_declared_pool(self) -> None:
        models = _models()
        deployment = models["catalog"][0]["deployments"][0]
        deployment["maxCapacity"] = 500
        deployment["maxCapacityPool"] = "global"
        required = preflight.catalog_requirements(
            models, capacity_profile="maximum"
        )["eastus2"][0]
        self.assertEqual(required["capacity"], 500)
        self.assertEqual(required["capacityPool"], "global")

    def test_declared_global_pool_over_limit_fails_non_vacuously(self) -> None:
        by_region = {
            "eastus2": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "eastus2",
                    "capacity": 200,
                    "capacityPool": "global",
                    "deploymentName": "east",
                }
            ],
            "swedencentral": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "swedencentral",
                    "capacity": 200,
                    "capacityPool": "global",
                    "deploymentName": "sweden",
                }
            ],
        }
        quota = preflight.index_quota(
            [
                {
                    "name": {"value": "OpenAI.GlobalStandard.model-a"},
                    "currentValue": 50,
                    "limit": 300,
                }
            ]
        )
        errors, _ = preflight.evaluate_declared_capacity_pools(by_region, quota)
        self.assertTrue(any("maximum profile requests 400" in error for error in errors))

    def test_partner_global_guard_rejects_mislabeled_regional_pools(self) -> None:
        by_region = {
            "eastus2": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "eastus2",
                    "deploymentName": "east",
                    "capacity": 300,
                    "capacityPool": "region:eastus2",
                }
            ],
            "swedencentral": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "swedencentral",
                    "deploymentName": "sweden",
                    "capacity": 300,
                    "capacityPool": "region:swedencentral",
                }
            ],
        }
        quota = preflight.index_quota(
            [
                {
                    "name": {"value": "AIServices.GlobalStandard.model-a"},
                    "currentValue": 50,
                    "limit": 300,
                }
            ]
        )
        errors, _ = preflight.evaluate_declared_capacity_pools(by_region, quota)
        self.assertTrue(
            any("subscription-global partner limit" in error for error in errors)
        )

    def test_missing_maximum_pool_uses_baseline_fallback(self) -> None:
        by_region = {
            "eastus2": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "eastus2",
                    "deploymentName": "east",
                    "capacity": 50,
                    "capacityPool": None,
                }
            ]
        }
        quota = preflight.index_quota(
            [
                {
                    "name": {"value": "AIServices.GlobalStandard.model-a"},
                    "currentValue": 50,
                    "limit": 300,
                }
            ]
        )
        errors, warnings = preflight.evaluate_declared_capacity_pools(
            by_region, quota
        )
        self.assertEqual(errors, [])
        self.assertTrue(any("falls back to baseline" in warning for warning in warnings))

    def test_partner_global_guard_includes_mixed_maximum_and_fallback_items(self) -> None:
        by_region = {
            "eastus2": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "eastus2",
                    "deploymentName": "east",
                    "capacity": 300,
                    "capacityPool": "global",
                }
            ],
            "swedencentral": [
                {
                    "name": "model-a",
                    "sku": "GlobalStandard",
                    "region": "swedencentral",
                    "deploymentName": "sweden",
                    "capacity": 50,
                    "capacityPool": None,
                }
            ],
        }
        quota = preflight.index_quota(
            [
                {
                    "name": {"value": "AIServices.GlobalStandard.model-a"},
                    "currentValue": 50,
                    "limit": 300,
                }
            ]
        )
        errors, _ = preflight.evaluate_declared_capacity_pools(by_region, quota)
        self.assertTrue(
            any(
                "350 total across all profile deployments" in error
                for error in errors
            )
        )


if __name__ == "__main__":
    unittest.main()
