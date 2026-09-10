"""Capacity-profile allocation and IaC wiring contracts."""

from __future__ import annotations

import json
import copy
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tests._loader import load_script
from scripts.tests._production_fixture import SUBSCRIPTION, production_document

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _capacity_evidence as evidence
import _production_capacity as production

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync-model-capacity.py"
PREFLIGHT = ROOT / "scripts" / "check-model-availability.py"

capacity = load_script("sync_model_capacity", SCRIPT)
preflight = load_script(
    "check_model_availability_capacity_profile",
    PREFLIGHT,
)
validator = load_script("production_feature_prereqs", ROOT / "scripts" / "validate-feature-prereqs.py")
generator = load_script("production_catalog_generator", ROOT / "scripts" / "gen-model-catalog.py")


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
        self.assertIn("capacity: selectedCapacity(d, modelCapacityProfile)", main)
        selection = (ROOT / "infra" / "capacity.bicep").read_text(encoding="utf-8")
        self.assertIn("deployment.?maxCapacity ?? deployment.capacity", selection)
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


class ProductionProfileTests(unittest.TestCase):
    def test_configured_selection_and_baseline_maximum_parity(self):
        models = production_document()
        for profile, expected in (("baseline", [10, 10]), ("production", [40, 20]), ("maximum", [100, 100])):
            requirements = preflight.catalog_requirements(models, capacity_profile=profile)
            self.assertEqual([r["capacity"] for rows in requirements.values() for r in rows], expected)
        del models["catalog"][0]["deployments"][0]["maxCapacity"]
        self.assertEqual(preflight.catalog_requirements(models, capacity_profile="maximum")["eastus2"][0]["capacity"], 10)
        self.assertEqual(preflight.catalog_requirements(models, capacity_profile="production")["eastus2"][0]["capacity"], 40)

    def test_missing_policy_review_selection_and_partial_configuration_refuse(self):
        cases = [
            ("productionCapacityPolicy",),
            ("productionCapacityPolicy", "review"),
            ("catalog", 0, "deployments", 0, "production"),
            ("catalog", 0, "deployments", 0, "production", "capacity"),
        ]
        for path in cases:
            with self.subTest(path=path):
                models = production_document()
                parent = models
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with self.assertRaises(evidence.EvidenceError):
                    production.parse_policy(models, required=True)
                with self.assertRaises(evidence.EvidenceError):
                    preflight.catalog_requirements(models, capacity_profile="production")
                if path[0] != "productionCapacityPolicy" or len(path) > 1:
                    self.assertEqual(preflight.catalog_requirements(models)["eastus2"][0]["capacity"], 10)

    def test_schema_and_source_validate_configured_fixture(self):
        import jsonschema

        schema = json.loads((ROOT / "infra" / "models.schema.json").read_text())
        models = production_document()
        jsonschema.Draft7Validator(schema).validate(models)
        self.assertIsNotNone(production.parse_policy(models, required=True))
        for value in (None, 0, -1, True, 1.5, 2**53):
            changed = copy.deepcopy(models)
            changed["catalog"][0]["deployments"][0]["production"]["capacity"] = value
            with self.subTest(value=value):
                with self.assertRaises(evidence.EvidenceError):
                    production.parse_policy(changed, required=True)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.Draft7Validator(schema).validate(changed)
        models["productionCapacityPolicy"]["unexpected"] = "typo"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft7Validator(schema).validate(models)
        with self.assertRaises(evidence.EvidenceError):
            production.parse_policy(models)

    def test_source_bounds_criticality_and_reserves_are_explicit(self):
        for field, value in (
            ("capacity", 29), ("capacity", 101), ("criticalMinimum", 101),
            ("critical", "yes"), ("poolId", "not-a-pool"),
        ):
            models = production_document()
            models["catalog"][0]["deployments"][0]["production"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(evidence.EvidenceError):
                production.parse_policy(models, required=True)
        models = production_document()
        models["productionCapacityPolicy"]["pools"][0]["reserve"]["replacement"] = 39
        with self.assertRaisesRegex(evidence.EvidenceError, "production_replacement_reserve_too_small"):
            production.parse_policy(models, required=True)
        models["productionCapacityPolicy"]["pools"][0]["reserve"]["replacement"] = 40
        self.assertIsNotNone(production.parse_policy(models, required=True))
        models["catalog"][0]["deployments"][1]["production"]["capacity"] = 41
        with self.assertRaisesRegex(evidence.EvidenceError, "production_replacement_reserve_too_small"):
            production.parse_policy(models, required=True)

    def test_source_rejects_unbound_naming_and_future_review(self):
        models = production_document()
        models["naming"]["pattern"] = "{model}-{region}"
        with self.assertRaisesRegex(evidence.EvidenceError, "unsupported_production_naming_pattern"):
            production.parse_policy(models, required=True)
        models = production_document()
        models["productionCapacityPolicy"]["review"]["reviewedAt"] = "2999-01-01T00:00:00Z"
        with self.assertRaisesRegex(evidence.EvidenceError, "production_review_in_future"):
            production.parse_policy(models, required=True)

    def test_overlapping_counter_or_split_version_policies_are_rejected(self):
        models = production_document()
        extra = copy.deepcopy(models["productionCapacityPolicy"]["pools"][0])
        extra["id"] = "second"
        models["productionCapacityPolicy"]["pools"].append(extra)
        with self.assertRaisesRegex(evidence.EvidenceError, "overlapping_pool_membership"):
            production.parse_policy(models)
        models = production_document()
        models["productionCapacityPolicy"]["pools"][0]["pool"]["model"]["versions"] = ["1"]
        with self.assertRaisesRegex(evidence.EvidenceError, "pool_version_membership_mismatch"):
            production.parse_policy(models)

    def test_disabled_anthropic_does_not_become_an_enabled_missing_deployment(self):
        models = production_document()
        extra = copy.deepcopy(models["catalog"][0])
        extra.update(name="partner-model", format="Anthropic", api="anthropic")
        for deployment in extra["deployments"]:
            del deployment["production"]
        models["catalog"].append(extra)
        selected = preflight.catalog_requirements(models, capacity_profile="production", include_anthropic=False)
        self.assertEqual(sum(map(len, selected.values())), 2)
        with self.assertRaisesRegex(evidence.EvidenceError, "production_deployment_not_configured"):
            preflight.catalog_requirements(models, capacity_profile="production", include_anthropic=True)

    def test_runtime_catalog_is_allocation_independent(self):
        models = production_document()
        declared = generator.build_catalog(models)
        without_policy = copy.deepcopy(models)
        del without_policy["productionCapacityPolicy"]
        for deployment in without_policy["catalog"][0]["deployments"]:
            del deployment["production"]
        self.assertEqual(declared, generator.build_catalog(without_policy))
        for option in declared["models"][0]["options"]:
            self.assertNotIn("capacity", option)
            self.assertNotIn("production", option)

    def test_real_azd_parameter_validator_refuses_unconfigured_production(self):
        parameters = ROOT / "infra" / "main.parameters.json"
        for profile, expected in (("baseline", 0), ("maximum", 0), ("production", 1)):
            with patch.dict(os.environ, {"AI4IA_MODEL_CAPACITY_PROFILE": profile}, clear=True), \
                 patch.object(validator, "PARAMETERS_FILE", parameters), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(validator.main(), expected)
            if expected:
                self.assertIn("production_policy_not_configured", err.getvalue())

    def test_configured_production_through_normal_deployment_parameter_gate(self):
        from scripts.tests.test_feature_prereqs import PROD_ENV

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(production_document(include_cu=True)), encoding="utf-8")
            values = {
                **PROD_ENV, "AI4IA_MODEL_CAPACITY_PROFILE": "production",
                "AZURE_ENV_NAME": "example", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                "AI4IA_WORKLOAD": "demo",
            }
            for environment, expected in (("example", 0), ("other", 1)):
                with patch.object(validator, "MODELS_FILE", path), \
                     patch.dict(os.environ, {**values, "AZURE_ENV_NAME": environment}, clear=True), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(validator.main(require_deployment_attestation=True), expected, err.getvalue())
                if expected:
                    self.assertIn("production_scope_mismatch", err.getvalue())

    def test_unconfigured_production_refuses_before_credential_or_arm_reads_with_control(self):
        class AzureReached(Exception):
            pass

        for profile in production.PROFILES:
            with patch.dict(os.environ, {"AI4IA_MODEL_CAPACITY_PROFILE": profile}, clear=True), \
                 patch.object(sys, "argv", ["check-model-availability.py"]), \
                 patch.object(preflight, "active_subscription", side_effect=AzureReached) as azure, \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                if profile == "production":
                    self.assertEqual(preflight.main(), 1)
                    azure.assert_not_called()
                    self.assertIn("production_policy_not_configured", err.getvalue())
                else:
                    with self.assertRaises(AzureReached):
                        preflight.main()
                    azure.assert_called_once()

    def test_full_normal_preflight_uses_explicit_pools_not_legacy_heuristics(self):
        models = production_document()
        policy = production.parse_policy(models, required=True)
        self.assertIsNotNone(policy)
        calls = []
        accounts = {
            region: f"mf-demo-example-{region}-abcdefghijklm" for region in models["regions"]
        }
        live = {}
        for d in policy.scope.catalog.deployments:
            live[d.region] = [{
                "name": d.name, "sku": {"name": d.model.sku, "capacity": 50},
                "properties": {
                    "model": {"name": d.model.name, "format": d.model.format, "version": d.model.version},
                    "provisioningState": "Succeeded", "versionUpgradeOption": "NoAutoUpgrade",
                },
            }]

        def azure(*args):
            calls.append(args)
            if args[:2] == ("account", "show"):
                result = {"id": SUBSCRIPTION, "name": "fixture"}
            elif args[:2] == ("group", "exists"):
                result = True
            elif args[:3] == ("cognitiveservices", "account", "list"):
                result = [{"name": name, "kind": "AIServices", "location": region} for region, name in accounts.items()]
            elif args[:4] == ("cognitiveservices", "account", "deployment", "list"):
                region = next(region for region, name in accounts.items() if name == args[args.index("--name") + 1])
                result = live[region]
            else:
                raise AssertionError(f"unapproved test operation: {args}")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(result), stderr="")

        offered = [
            {"model": {
                "format": d.model.format, "name": d.model.name, "version": d.model.version,
                "lifecycleStatus": "GenerallyAvailable", "skus": [{"name": d.model.sku}],
            }} for d in policy.scope.catalog.deployments
        ]
        quota = [{"name": {"value": policy.pools[0].assertion["counter"]}, "unit": "Count", "currentValue": 140, "limit": 300}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(models), encoding="utf-8")
            env = {
                "AI4IA_MODEL_CAPACITY_PROFILE": "production", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                "AZURE_ENV_NAME": "example", "AI4IA_WORKLOAD": "demo",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(preflight, "MODELS_FILE", path), \
                 patch.object(sys, "argv", ["check-model-availability.py"]), patch.object(preflight, "_az", side_effect=azure), \
                 patch.object(preflight, "offered_models", return_value=offered), \
                 patch.object(preflight, "quota_usage", return_value=quota), \
                 patch.object(preflight, "evaluate_shared_quota", side_effect=AssertionError("heuristic forbidden")), \
                 patch.object(preflight, "evaluate_declared_capacity_pools", side_effect=AssertionError("maximum forbidden")):
                for limit, expected in ((300, 0), (190, 0), (189, 1)):
                    quota[0]["limit"] = limit
                    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(preflight.main(), expected)
                    if expected == 0:
                        self.assertIn("operator_asserted", out.getvalue())
                        self.assertIn("selected 60", out.getvalue())
                        self.assertIn("reserved 90", out.getvalue())
                    else:
                        self.assertIn("production_pool_reserve_exceeded", out.getvalue())
                quota[0]["limit"] = 300
                live["swedencentral"][0]["properties"]["model"]["version"] = "unreviewed"
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(preflight.main(), 1)
                self.assertIn("unsettled_or_unreviewed_pool_deployment", out.getvalue())
            self.assertTrue(any(c[:4] == ("cognitiveservices", "account", "deployment", "list") for c in calls))
            self.assertEqual({c[c.index("--name") + 1] for c in calls if c[:4] == ("cognitiveservices", "account", "deployment", "list")}, set(accounts.values()))
            for change in ({"AZURE_SUBSCRIPTION_ID": "22222222-2222-2222-2222-222222222222"}, {"AZURE_ENV_NAME": "other"}):
                with patch.dict(os.environ, {**env, **change}, clear=True), patch.object(preflight, "MODELS_FILE", path), \
                     patch.object(sys, "argv", ["check-model-availability.py"]), \
                     patch.object(preflight, "_az", side_effect=AssertionError("must refuse before Azure")), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(preflight.main(), 1)
            for flags in (["--skip-quota"], ["--region", "eastus2"]):
                with patch.dict(os.environ, env, clear=True), patch.object(preflight, "MODELS_FILE", path), \
                     patch.object(sys, "argv", ["check-model-availability.py", *flags]), \
                     patch.object(preflight, "_az", side_effect=AssertionError("must refuse before Azure")), \
                     contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(preflight.main(), 1)
                self.assertIn("production_requires_all_pool_quota_reads", err.getvalue())

    def test_unconfigured_retirement_profile_is_unknown_not_attention(self):
        with patch.dict(os.environ, {"AI4IA_MODEL_CAPACITY_PROFILE": "production"}, clear=True), \
             patch.object(sys, "argv", ["check-model-availability.py", "--retirement-report", "unused"]), \
             patch.object(preflight, "_az", side_effect=AssertionError("Azure forbidden")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(preflight.main(), 2)

    def test_production_retirement_dispatch_never_enters_quota_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(production_document()), encoding="utf-8")
            env = {
                "AI4IA_MODEL_CAPACITY_PROFILE": "production", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                "AZURE_ENV_NAME": "example", "AI4IA_WORKLOAD": "demo",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(preflight, "MODELS_FILE", path), \
                 patch.object(sys, "argv", ["check-model-availability.py", "--retirement-report", str(Path(directory) / "report")]), \
                 patch.object(preflight, "run_retirement_report", return_value=0) as report, \
                 patch.object(preflight, "_az", side_effect=AssertionError("only report reader may read Azure")), \
                 patch.object(preflight, "quota_usage", side_effect=AssertionError("retirement has no quota grant")):
                self.assertEqual(preflight.main(), 0)
            args, models, _bytes = report.call_args.args
            self.assertEqual(args.capacity_profile, "production")
            selected = preflight.catalog_requirements(models, capacity_profile=args.capacity_profile)
            self.assertEqual([d["capacity"] for rows in selected.values() for d in rows], [40, 20])


if __name__ == "__main__":
    unittest.main()
