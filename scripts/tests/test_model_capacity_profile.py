"""Capacity-profile allocation and IaC wiring contracts."""

from __future__ import annotations

import contextlib
import copy
import io
import json
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
    def test_staged_absence_and_retained_refusal_in_both_planners(self):
        for planner in ("maximum", "reduce"):
            models = QuotaReductionTests().models()
            staged = copy.deepcopy(models["catalog"][0])
            staged.update(name="staged-video", runtimeEnabled=False, provisioningEnabled=False)
            models["catalog"].append(staged)
            def run(document, live):
                if planner == "reduce":
                    return capacity.reduce_capacity_to_quota(document, live, _quota("GlobalStandard", 100, 1000))
                return capacity.build_capacity_plan(
                    document, live, _quota("GlobalStandard", 100, 1000), _platform("GlobalStandard", 900, 900),
                )
            with self.subTest(planner=planner):
                expected = run({**models, "catalog": models["catalog"][:1]}, _live("GlobalStandard"))
                self.assertEqual(run(models, _live("GlobalStandard")), expected)
                with self.assertRaisesRegex(ValueError, "complete deployment inventory"):
                    run(models, {})
                for runtime in (False, True):
                    staged["runtimeEnabled"] = runtime
                    live = _live("GlobalStandard")
                    for d in staged["deployments"]:
                        live[d["region"]][capacity.deployment_name(models, staged["name"], d)] = 100
                    with self.assertRaisesRegex(ValueError, "retained provisioning-disabled"):
                        run(models, live)
                with self.assertRaisesRegex(ValueError, "required externally managed"):
                    run(models, _live("GlobalStandard"))
                for field in ("runtimeEnabled", "provisioningEnabled"):
                    for value in (None, "false", 0):
                        malformed = copy.deepcopy(models)
                        malformed["catalog"][0][field] = value
                        with self.assertRaises(ValueError):
                            run(malformed, _live("GlobalStandard"))
                required = copy.deepcopy(models)
                required["catalog"] = required["catalog"][:1]
                with self.assertRaisesRegex(ValueError, "required externally managed or provisioned"):
                    run(required, {region: {} for region in required["regions"]})

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

    def test_publisher_does_not_override_contradictory_shared_counter_usage(self) -> None:
        with self.assertRaisesRegex(ValueError, "shared-counter membership"):
            capacity.build_capacity_plan(
                _models(), _live("GlobalStandard"),
                _quota("GlobalStandard", current=50, limit=300, publisher="AIServices"),
                _platform("GlobalStandard", east=250, sweden=250),
            )

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


class SyncStagedMainTests(unittest.TestCase):
    def run_sync(self, models, *, apply=False, reduce=False, corruption=None, absent=False, inventory="complete"):
        models["naming"]["foundryToken"] = "demo"
        accounts = [
            {"name": f"mf-demo-example-{region}-abcdefghijklm", "kind": "AIServices", "location": region}
            for region in models["regions"]
        ]
        def azure(*args):
            if args[:2] == ("account", "show"):
                return {"id": SUBSCRIPTION}
            if args[:3] == ("cognitiveservices", "account", "list"):
                if inventory == "failed":
                    raise RuntimeError("mock failed account inventory")
                if inventory == "continued":
                    return {"value": accounts, "nextLink": "unconsumed"}
                if inventory == "empty":
                    return []
                return accounts
            if args[:4] == ("cognitiveservices", "account", "deployment", "list"):
                account = args[args.index("--name") + 1]
                region = next(a["location"] for a in accounts if a["name"] == account)
                rows = []
                for model in models["catalog"]:
                    if absent and not model.get("provisioningEnabled", True):
                        continue
                    for d in model["deployments"]:
                        if d["region"] != region:
                            continue
                        name = capacity.deployment_name(models, model["name"], d)
                        rows.append({
                            "name": name, "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-demo-example/"
                            f"providers/Microsoft.CognitiveServices/accounts/{account}/deployments/{name}",
                            "sku": {"name": d["sku"], "capacity": 100},
                            "properties": {"model": {"format": model["format"], "name": model["name"], "version": d["version"]},
                                           "provisioningState": "Succeeded"},
                        })
                if corruption and rows:
                    row = rows[-1]
                    if corruption == "id":
                        row["id"] = row["id"].replace(account, "foreign-account")
                    elif corruption in ("format", "name", "version"):
                        row["properties"]["model"][corruption] = "wrong"
                    elif corruption == "sku":
                        row["sku"]["name"] = "Standard"
                    elif corruption == "state":
                        row["properties"]["provisioningState"] = "Failed"
                    elif corruption == "duplicate":
                        rows.append(copy.deepcopy(row))
                    elif corruption == "capacity":
                        row["sku"]["capacity"] = True
                    elif corruption == "outside":
                        outside = copy.deepcopy(row)
                        outside["name"] += "-outside"
                        outside["id"] += "-outside"
                        rows.append(outside)
                    elif corruption == "continued":
                        return {"value": rows, "nextLink": "unconsumed"}
                return rows
            if args[:3] == ("cognitiveservices", "usage", "list"):
                return _quota("GlobalStandard", 200, 300)["eastus2"]
            if args[:1] == ("rest",):
                return _platform("GlobalStandard", 100, 100)[("OpenAI", "model-a", "1")]
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            plan = Path(directory) / "plan.json"
            original = json.dumps(models).encode()
            path.write_bytes(original)
            args = ["sync-model-capacity.py", "--resource-group", "rg-demo-example",
                    "--environment-name", "example", "--output-plan", str(plan)]
            args += ["--apply"] if apply else []
            args += ["--reduce-to-quota"] if reduce else []
            with patch.object(capacity, "MODELS_FILE", path), patch.object(capacity, "ROOT", Path(directory)), \
                 patch.object(capacity, "_az_json", side_effect=azure), patch.object(sys, "argv", args), \
                 contextlib.redirect_stdout(io.StringIO()):
                try:
                    code = capacity.main()
                except (ValueError, RuntimeError) as exc:
                    self.assertEqual(path.read_bytes(), original)
                    self.assertFalse(plan.exists())
                    return str(exc)
            return code

    def test_retained_refuses_before_catalog_or_plan_write_with_valid_control(self):
        for runtime in (False, True):
            for apply in (False, True):
                for reduce in (False, True):
                    for corruption in (None, "id", "format", "name", "version", "sku", "state", "duplicate", "capacity", "continued"):
                        with self.subTest(runtime=runtime, apply=apply, reduce=reduce, corruption=corruption):
                            models = _models()
                            models["catalog"][0].update(provisioningEnabled=False, runtimeEnabled=runtime)
                            self.assertIsInstance(self.run_sync(models, apply=apply, reduce=reduce, corruption=corruption), str)
        models = _models()
        models["catalog"][0].update(provisioningEnabled=True, runtimeEnabled=False)
        self.assertEqual(self.run_sync(models, apply=True), 0)

    def test_optional_absent_candidate_keeps_existing_plan_and_apply(self):
        for apply in (False, True):
            for reduce in (False, True):
                models = _models()
                candidate = copy.deepcopy(models["catalog"][0])
                candidate.update(name="staged-video", runtimeEnabled=False, provisioningEnabled=False)
                models["catalog"].append(candidate)
                self.assertEqual(self.run_sync(models, apply=apply, reduce=reduce, absent=True), 0)

    def test_provisioned_allocations_require_exact_identity_before_quota_arithmetic(self):
        for apply in (False, True):
            for reduce in (False, True):
                for corruption in ("id", "format", "name", "version", "sku", "state", "duplicate", "capacity", "outside"):
                    with self.subTest(apply=apply, reduce=reduce, corruption=corruption):
                        models = _models()
                        self.assertIsInstance(self.run_sync(
                            models, apply=apply, reduce=reduce, corruption=corruption,
                        ), str)
                self.assertEqual(self.run_sync(_models(), apply=apply, reduce=reduce), 0)

    def test_optional_absence_requires_complete_inventory_and_required_rows_refuse(self):
        for apply in (False, True):
            for reduce in (False, True):
                for runtime in (False, True):
                    models = _models()
                    models["catalog"][0].update(provisioningEnabled=False, runtimeEnabled=runtime)
                    for inventory in ("failed", "continued"):
                        self.assertIsInstance(self.run_sync(
                            models, apply=apply, reduce=reduce, inventory=inventory,
                        ), str)
                    result = self.run_sync(models, apply=apply, reduce=reduce, inventory="empty")
                    if runtime:
                        self.assertIsInstance(result, str)
                    else:
                        self.assertEqual(result, 0)


class QuotaReductionTests(unittest.TestCase):
    def models(self, sku="GlobalStandard"):
        models = _models(sku)
        for deployment in models["catalog"][0]["deployments"]:
            deployment["maxCapacity"] = 500
            deployment["maxCapacityPool"] = (
                f"data-zone:{models['regions'][deployment['region']]['dataZone']}"
                if sku == "DataZoneStandard" else "global"
            )
        return models

    def test_recorded_global_pool_is_reduced_without_platform_increase_or_reassignment(self):
        models = self.models()
        before = copy.deepcopy(models)
        plan, pools, diagnostics = capacity.reduce_capacity_to_quota(
            models, _live("GlobalStandard", 500, 500),
            _quota("GlobalStandard", 1000, 250),
        )
        self.assertEqual(set(plan.values()), {125})
        self.assertEqual(sum(plan.values()), 250)
        self.assertEqual(set(pools.values()), {"global"})
        self.assertIn("outside allocation 0", diagnostics[0])
        self.assertEqual(models, before)

    def test_outside_allocation_is_not_spent_by_the_reduction(self):
        with self.assertRaisesRegex(ValueError, "outside allocation.*unproven"):
            capacity.reduce_capacity_to_quota(
                self.models(), _live("GlobalStandard", 500, 500),
                _quota("GlobalStandard", 1050, 250),
            )

    def test_data_zones_remain_independent_and_healthy_pools_are_untouched(self):
        plan, pools, _ = capacity.reduce_capacity_to_quota(
            self.models("DataZoneStandard"), _live("DataZoneStandard", 500, 500),
            _quota("DataZoneStandard", 500, 100),
        )
        self.assertEqual(set(plan.values()), {100})
        self.assertEqual(set(pools.values()), {"data-zone:US", "data-zone:EU"})
        self.assertEqual(capacity.reduce_capacity_to_quota(
            self.models(), _live("GlobalStandard", 500, 500),
            _quota("GlobalStandard", 1000, 1000),
        ), ({}, {}, []))

    def test_unknown_contradictory_or_insufficient_evidence_refuses(self):
        for change in ("missing", "replicas", "usage", "baseline", "live", "pool"):
            models = self.models()
            live = _live("GlobalStandard", 500, 500)
            quota = _quota("GlobalStandard", 1000, 250)
            if change == "missing":
                quota["eastus2"] = []
            elif change == "replicas":
                quota["swedencentral"] = [{**quota["swedencentral"][0], "limit": 300}]
            elif change == "usage":
                quota = _quota("GlobalStandard", 999, 250)
            elif change == "baseline":
                quota = _quota("GlobalStandard", 1000, 99)
            elif change == "live":
                live["eastus2"].clear()
            else:
                for deployment in models["catalog"][0]["deployments"]:
                    deployment["maxCapacityPool"] = "unrecognized"
            with self.subTest(change=change), self.assertRaises(ValueError):
                capacity.reduce_capacity_to_quota(models, live, quota)


class SyncCounterSafetyTests(unittest.TestCase):
    def fixture(self, *, reduce=False, staged=False, outside=True):
        models = QuotaReductionTests().models() if reduce else _models()
        if reduce:
            for d in models["catalog"][0]["deployments"]:
                d["maxCapacity"] = 80
        if staged:
            candidate = copy.deepcopy(models["catalog"][0])
            candidate.update(name="staged-video", runtimeEnabled=False, provisioningEnabled=False)
            models["catalog"].append(candidate)
        live = _live("GlobalStandard", 100, 100) if reduce else _live("GlobalStandard")
        current = (200 if reduce else 100) + ((20 if reduce else 40) if outside else 0)
        quota = _quota("GlobalStandard", current, 170, publisher="AIServices")
        return models, live, quota, _platform("GlobalStandard", 70, 70)

    def run_plan(self, models, live, quota, platform, *, reduce=False):
        if reduce:
            return capacity.reduce_capacity_to_quota(models, live, quota)
        return capacity.build_capacity_plan(models, live, quota, platform)

    def test_outside_allocation_refuses_before_actionable_plan_or_healthy_shortcut(self):
        for reduce in (False, True):
            for staged in (False, True):
                with self.subTest(reduce=reduce, staged=staged):
                    fixture = self.fixture(reduce=reduce, staged=staged)
                    original = copy.deepcopy(fixture)
                    with self.assertRaisesRegex(ValueError, "outside allocation.*unproven"):
                        self.run_plan(*fixture, reduce=reduce)
                    self.assertEqual(fixture, original)
                    models, live, quota, platform = self.fixture(reduce=reduce, staged=staged, outside=False)
                    plan, _, _ = self.run_plan(models, live, quota, platform, reduce=reduce)
                    self.assertEqual(sum(plan.values()), 0 if reduce else 170)
                    # Even a numerically exact outside boundary lacks proven pool membership.
                    if reduce:
                        for d in models["catalog"][0]["deployments"]:
                            d["maxCapacity"] = 75
                        with self.assertRaisesRegex(ValueError, "outside allocation"):
                            self.run_plan(models, live, fixture[2], platform, reduce=True)

    def test_dry_run_and_temp_apply_refuse_without_writing_and_zero_outside_control_runs(self):
        for reduce in (False, True):
            for staged in (False, True):
                for apply in (False, True):
                    for outside in (False, True):
                        with self.subTest(reduce=reduce, staged=staged, apply=apply, outside=outside):
                            models, live, quota, platform = self.fixture(reduce=reduce, staged=staged, outside=outside)
                            with tempfile.TemporaryDirectory() as directory:
                                catalog, output = Path(directory) / "models.json", Path(directory) / "plan.json"
                                original = json.dumps(models)
                                catalog.write_text(original, encoding="utf-8")
                                def azure(*args, quota=quota):
                                    if args[:2] == ("account", "show"):
                                        return {"id": SUBSCRIPTION}
                                    if args[:3] == ("cognitiveservices", "usage", "list"):
                                        return quota[args[args.index("--location") + 1]]
                                    raise AssertionError(args)
                                argv = [
                                    "sync-model-capacity.py", "--resource-group", "rg-demo-example",
                                    "--environment-name", "example", "--output-plan", str(output),
                                ]
                                if apply:
                                    argv.append("--apply")
                                if reduce:
                                    argv.append("--reduce-to-quota")
                                with (
                                    patch.object(capacity, "MODELS_FILE", catalog),
                                    patch.object(capacity, "ROOT", Path(directory)),
                                    patch.object(capacity, "_collect_live", return_value=live),
                                    patch.object(capacity, "_collect_platform", return_value=platform),
                                    patch.object(capacity, "_az_json", side_effect=azure),
                                    patch.object(sys, "argv", argv),
                                    contextlib.redirect_stdout(io.StringIO()),
                                ):
                                    if outside:
                                        with self.assertRaisesRegex(ValueError, "outside allocation"):
                                            capacity.main()
                                    else:
                                        self.assertEqual(capacity.main(), 0)
                                if outside:
                                    self.assertEqual(catalog.read_text(encoding="utf-8"), original)
                                    self.assertFalse(output.exists())
                                else:
                                    self.assertTrue(output.exists())

    def test_quota_current_and_limit_are_known_native_counts_not_defaults(self):
        for reduce in (False, True):
            for field in ("currentValue", "limit"):
                for value in ("missing", None, True, -1, 100.5, "100"):
                    models, live, quota, platform = self.fixture(reduce=reduce, outside=False)
                    for rows in quota.values():
                        if value == "missing":
                            rows[0].pop(field, None)
                        else:
                            rows[0][field] = value
                    with self.subTest(reduce=reduce, field=field, value=value):
                        with self.assertRaisesRegex(ValueError, "known nonnegative integers"):
                            self.run_plan(models, live, quota, platform, reduce=reduce)

    def test_duplicate_quota_continuation_and_platform_evidence_are_not_projected_away(self):
        models, live, quota, platform = self.fixture(outside=False)
        for region in quota:
            quota[region].append(copy.deepcopy(quota[region][0]))
        with self.assertRaisesRegex(ValueError, "duplicate or aliased"):
            self.run_plan(models, live, quota, platform)
        with self.assertRaisesRegex(TypeError, "Complete quota"):
            capacity.index_quota({"value": [], "nextLink": "continued"})
        for value in (None, True, -1, 70.5, "70"):
            models, live, quota, platform = self.fixture(outside=False)
            next(iter(platform.values()))["value"][0]["properties"]["availableCapacity"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be explicit"):
                self.run_plan(models, live, quota, platform)
        with self.assertRaisesRegex(ValueError, "Complete platform"):
            capacity._platform_index({"value": [], "nextLink": "continued"})

    def test_versions_cannot_spend_the_same_counter_twice(self):
        for reduce in (False, True):
            models, live, quota, platform = self.fixture(reduce=reduce, outside=False)
            models["catalog"][0]["deployments"][1]["version"] = "2"
            platform[("OpenAI", "model-a", "2")] = copy.deepcopy(platform[("OpenAI", "model-a", "1")])
            with self.subTest(reduce=reduce), self.assertRaisesRegex(ValueError, "Multiple model/version"):
                self.run_plan(models, live, quota, platform, reduce=reduce)


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


class RetainedProductionPreflightTests(unittest.TestCase):
    def fixture(self, runtime=False, provisioning=False):
        models = production_document()
        models["catalog"][0].update(runtimeEnabled=runtime, provisioningEnabled=provisioning)
        policy = production.parse_policy(models, required=True, include_anthropic=False)
        assert policy is not None
        inventory = {}
        for d in policy.scope.catalog.deployments:
            account = f"mf-demo-example-{d.region}-abcdefghijklm"
            inventory[(d.region, d.name.casefold())] = {
                "id": f"{policy.scope.group_path}/providers/Microsoft.CognitiveServices/accounts/{account}/deployments/{d.name}",
                "accountName": account, "region": d.region, "deploymentName": d.name,
                "format": d.model.format, "modelName": d.model.name, "version": d.model.version,
                "sku": d.model.sku, "capacity": 50, "provisioningState": "Succeeded",
                "versionUpgradeOption": "NoAutoUpgrade",
            }
        quotas = {
            region: [{
                "counter": policy.pools[0].assertion["counter"],
                "unit": "Count", "currentValue": 140, "limit": 300,
            }] for region in models["regions"]
        }
        return models, policy, inventory, quotas

    def run_main(
        self, models, inventory, *, limit=300, failure=None, malformed=None,
        profile="production", failure_detail="fixture read failed",
    ):
        calls = []

        def azure(*args):
            calls.append(args)
            operation = args[:4]
            if args[:2] == ("account", "show"):
                result = {"id": SUBSCRIPTION, "name": "fixture"}
            elif args[:2] == ("group", "exists"):
                result = True
            elif args[:3] == ("cognitiveservices", "account", "list"):
                result = [
                    {"name": f"mf-demo-example-{region}-abcdefghijklm", "kind": "AIServices", "location": region}
                    for region in models["regions"]
                ]
                if malformed == "duplicate-account":
                    result.append(dict(result[0]))
                elif malformed == "continued-accounts":
                    result = {"value": result, "nextLink": "unconsumed"}
                elif malformed == "empty-accounts":
                    result = []
                elif isinstance(malformed, tuple):
                    field, value = malformed
                    for account in result:
                        if value is None:
                            account.pop(field)
                        else:
                            account[field] = value
            elif operation == ("cognitiveservices", "account", "deployment", "list"):
                name = args[args.index("--name") + 1]
                if failure == name:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr=failure_detail)
                result = [{
                    "id": row["id"], "name": row["deploymentName"],
                    "sku": {"name": row["sku"], "capacity": row["capacity"]},
                    "properties": {
                        "model": {"format": row["format"], "name": row["modelName"], "version": row["version"]},
                        "provisioningState": row["provisioningState"],
                        "versionUpgradeOption": row["versionUpgradeOption"],
                    },
                } for row in inventory.values() if row["accountName"] == name]
                if malformed == "continued-deployments":
                    result = {"value": result, "nextLink": "unconsumed"}
                elif malformed == "duplicate-deployment" and result:
                    result.append(dict(result[0]))
            else:
                raise AssertionError(f"unapproved test operation: {args}")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(result), stderr="")

        offered = [{
            "model": {
                "format": m["format"], "name": m["name"], "version": d["version"],
                "lifecycleStatus": "GenerallyAvailable", "skus": [{"name": d["sku"]}],
            },
        } for m in models["catalog"] for d in m["deployments"]]
        quota = [{
            "name": {"value": models["productionCapacityPolicy"]["pools"][0]["pool"]["counter"]},
            "unit": "Count", "currentValue": 140, "limit": limit,
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(models), encoding="utf-8")
            env = {
                "AI4IA_MODEL_CAPACITY_PROFILE": profile, "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                "AZURE_ENV_NAME": "example", "AI4IA_WORKLOAD": "demo",
            }
            if malformed == "stale-endpoints":
                env["AZURE_FOUNDRY_ENDPOINTS"] = json.dumps([
                    {"region": region, "accountName": f"mf-demo-example-{region}-nonexistent"}
                    for region in models["regions"]
                ])
            with patch.dict(os.environ, env, clear=True), patch.object(preflight, "MODELS_FILE", path), \
                 patch.object(sys, "argv", ["check-model-availability.py"]), \
                 patch.object(preflight, "_az", side_effect=azure), \
                 patch.object(preflight, "verify_configured", return_value=0), \
                 patch.object(preflight, "offered_models", return_value=offered) as offers, \
                 patch.object(preflight, "quota_usage", return_value=quota) as quotas, \
                 contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    code = preflight.main()
                except SystemExit as exc:
                    code = str(exc)
        return code, out.getvalue() + err.getvalue(), calls, offers.call_args_list, quotas.call_args_list

    def test_retained_capacity_matches_actual_bicep_and_postprovision_all_profiles(self):
        from scripts.tests.test_bicep_compilation import SourceInventoryCompiledTests
        from scripts.tests.test_postprovision_hard_gates import _run_model

        for profile in ("baseline", "production", "maximum"):
            for runtime in (False, True):
                for exact in (False, True):
                    with self.subTest(profile=profile, runtime=runtime, exact=exact):
                        models, policy, inventory, _ = self.fixture(runtime=runtime)
                        if profile == "maximum":
                            for deployment in models["catalog"][0]["deployments"]:
                                deployment["maxCapacity"] = 50
                        compiled = SourceInventoryCompiledTests().selections(models, profile)
                        for index, region in enumerate(sorted(models["regions"])):
                            evidence_row = compiled["evidence"][index][0]
                            row = next(item for item in inventory.values() if item["region"] == region)
                            row["capacity"] = evidence_row["capacity"] if exact else (
                                60 if profile == "maximum" else 50
                            )
                            account_id = policy.scope.account_path(row["accountName"])
                            target = {
                                "accountName": row["accountName"], "accountId": account_id,
                                "region": region, "verificationVersion": 1,
                                "deploymentNames": [],
                                "deploymentEvidence": compiled["evidence"][index],
                            }
                            actual = {
                                "name": row["deploymentName"], "id": row["id"],
                                "sku": {"name": row["sku"], "capacity": row["capacity"]},
                                "properties": {
                                    "model": evidence_row["model"], "provisioningState": "Succeeded",
                                    "versionUpgradeOption": row["versionUpgradeOption"],
                                },
                            }
                            gate = _run_model(overrides={
                                "AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps([target]),
                                "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                                "AZURE_RESOURCE_GROUP": policy.scope.resource_group,
                            }, deployments=[actual])
                            self.assertEqual(gate["results"][0]["status"], "PASS" if exact else "FAIL")
                        code, text, _, _, _ = self.run_main(models, inventory, profile=profile)
                        self.assertEqual(code, 0 if exact else 1, text)
                        if not exact:
                            self.assertIn("retained_deployment_capacity_mismatch", text)

    def test_retained_identity_matches_real_bicep_and_ps_gate_all_profiles(self):
        from scripts.tests.test_bicep_compilation import SourceInventoryCompiledTests
        from scripts.tests.test_postprovision_hard_gates import _run_model

        for profile in ("baseline", "maximum", "production"):
            for runtime in (False, True):
                models, policy, inventory, _ = self.fixture(runtime=runtime)
                compiled = SourceInventoryCompiledTests().selections(models, profile)
                for index, region in enumerate(sorted(models["regions"])):
                    inventory[(region, compiled["evidence"][index][0]["name"].casefold())]["capacity"] = (
                        compiled["evidence"][index][0]["capacity"]
                    )
                region = "eastus2"
                index = sorted(models["regions"]).index(region)
                key = region, compiled["evidence"][index][0]["name"].casefold()
                for field, value in (
                    (None, None), ("modelName", "different-model"), ("format", "different-format"),
                    ("version", "99"), ("sku", "Standard"), ("provisioningState", "Failed"),
                    ("versionUpgradeOption", "OnceNewDefaultVersionAvailable"),
                    ("id", inventory[key]["id"].replace(inventory[key]["accountName"], "foreign-account")),
                ):
                    with self.subTest(profile=profile, runtime=runtime, field=field):
                        actual_inventory = copy.deepcopy(inventory)
                        row = actual_inventory[key]
                        if field is not None:
                            row[field] = value
                        target = {
                            "region": region, "accountName": row["accountName"],
                            "accountId": policy.scope.account_path(row["accountName"]),
                            "verificationVersion": 1, "deploymentNames": [],
                            "deploymentEvidence": compiled["evidence"][index],
                        }
                        actual = {
                            "name": row["deploymentName"], "id": row["id"],
                            "sku": {"name": row["sku"], "capacity": row["capacity"]},
                            "properties": {
                                "model": {
                                    "name": row["modelName"], "format": row["format"], "version": row["version"],
                                },
                                "provisioningState": row["provisioningState"],
                                "versionUpgradeOption": row["versionUpgradeOption"],
                            },
                        }
                        gate = _run_model(overrides={
                            "AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps([target]),
                            "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                            "AZURE_RESOURCE_GROUP": policy.scope.resource_group,
                        }, deployments=[actual])
                        self.assertEqual(gate["results"][0]["status"], "PASS" if field is None else "FAIL")
                        code, text, _, offers, quota_reads = self.run_main(
                            models, actual_inventory, profile=profile,
                        )
                        self.assertEqual(code, 0 if field is None else 1, text)
                        if field is not None:
                            self.assertIn("retained_deployment_", text)
                            self.assertFalse(offers or quota_reads)

    def test_provisioned_identity_drift_still_reconciles_with_legacy_defaults(self):
        for profile in ("baseline", "maximum"):
            for runtime in ("omitted", False, True):
                with self.subTest(profile=profile, runtime=runtime):
                    models, _, inventory, _ = self.fixture(provisioning=True)
                    del models["catalog"][0]["provisioningEnabled"]
                    if runtime == "omitted":
                        del models["catalog"][0]["runtimeEnabled"]
                    else:
                        models["catalog"][0]["runtimeEnabled"] = runtime
                    for row in inventory.values():
                        row.update(provisioningState="Failed", versionUpgradeOption="OnceNewDefaultVersionAvailable")
                    code, text, _, _, _ = self.run_main(models, inventory, profile=profile)
                    self.assertEqual(code, 0, text)

    def test_retained_identity_requires_each_native_evidence_field(self):
        models, policy, inventory, _ = self.fixture()
        scope = policy.scope
        args = {
            "subscription": scope.subscription, "resource_group": scope.resource_group,
            "environment": scope.environment,
        }
        self.assertEqual(preflight.retained_source_identity_errors(models, inventory, **args), [])
        key = next(iter(inventory))
        for field in (
            "region", "deploymentName", "accountName", "id", "format", "modelName",
            "version", "sku", "provisioningState", "versionUpgradeOption",
        ):
            for value in (None, False, [], {}, 7):
                with self.subTest(field=field, value=value):
                    actual = copy.deepcopy(inventory)
                    actual[key][field] = value
                    errors = preflight.retained_source_identity_errors(models, actual, **args)
                    self.assertEqual(len(errors), 1)
                    self.assertIn("mismatch", errors[0])

    def test_account_filtering_cannot_establish_optional_absence(self):
        models, _, inventory, _ = self.fixture()
        for malformed in (
            ("name", None), ("name", ["account"]), ("name", 123), ("name", " "),
            ("kind", None), ("kind", ["AIServices"]), ("kind", "Other"),
            ("location", None), ("location", ["eastus2"]), ("location", 123),
            ("location", "east-us2"), ("location", "westus"),
            "stale-endpoints",
        ):
            with self.subTest(malformed=malformed):
                code, text, _, offers, quota_reads = self.run_main(
                    models, inventory, limit=190, malformed=malformed,
                )
                self.assertEqual(code, 1, text)
                self.assertIn("production_inventory_unknown", text)
                self.assertFalse(offers or quota_reads)
        code, text, calls, offers, quota_reads = self.run_main(
            models, inventory, limit=190, malformed="empty-accounts",
        )
        self.assertEqual(code, 0, text)
        self.assertFalse(offers or quota_reads)
        self.assertFalse(any(call[:4] == ("cognitiveservices", "account", "deployment", "list") for call in calls))

    def test_confirmed_optional_absence_skips_offers_in_all_profiles(self):
        for profile in ("baseline", "maximum", "production"):
            models, _, _, _ = self.fixture()
            for model in models["catalog"]:
                for deployment in model["deployments"]:
                    deployment.pop("production", None)
            code, text, _, offers, quota_reads = self.run_main(models, {}, profile=profile)
            self.assertEqual(code, 0, text)
            self.assertFalse(offers or quota_reads)
            for malformed in ("continued-accounts", "continued-deployments", ("kind", None)):
                code, text, _, offers, quota_reads = self.run_main(models, {}, profile=profile, malformed=malformed)
                self.assertNotEqual(code, 0, text)
                self.assertFalse(offers or quota_reads)
            models["catalog"][0]["runtimeEnabled"] = True
            code, text, _, _, _ = self.run_main(models, {}, profile=profile)
            self.assertNotEqual(code, 0, text)

    def test_endpoint_account_identity_requires_strings(self):
        entry = {"region": "eastus2", "accountName": "mf-demo-example-eastus2-abcdefghijklm"}
        self.assertEqual(preflight._foundry_accounts_from_output(json.dumps([entry])), {
            entry["region"]: entry["accountName"],
        })
        for field in entry:
            for value in (None, [], ["eastus2"], 123, True, ""):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(SystemExit):
                        preflight._foundry_accounts_from_output(json.dumps([{**entry, field: value}]))

    def test_retained_capacity_requires_native_positive_integer_evidence(self):
        for profile in ("baseline", "production", "maximum"):
            models, _, inventory, _ = self.fixture()
            requirements = preflight.catalog_requirements(models, capacity_profile=profile)
            for region, rows in requirements.items():
                inventory[(region, rows[0]["deploymentName"].casefold())]["capacity"] = rows[0]["capacity"]
            self.assertEqual(preflight.retained_capacity_errors(requirements, inventory), [])
            for side in ("expected", "actual"):
                for value in (None, True, False, 0, -1, 1.0, "40"):
                    with self.subTest(profile=profile, side=side, value=value):
                        desired, observed = copy.deepcopy(requirements), copy.deepcopy(inventory)
                        if side == "expected":
                            desired["eastus2"][0]["capacity"] = value
                        else:
                            next(iter(observed.values()))["capacity"] = value
                        errors = preflight.retained_capacity_errors(desired, observed)
                        self.assertEqual(len(errors), 1)
                        self.assertIn("retained_deployment_capacity_unknown", errors[0])

    def test_provisioned_capacity_drift_remains_reconcilable_in_all_profiles(self):
        for profile in ("baseline", "production", "maximum"):
            for legacy in (False, True):
                with self.subTest(profile=profile, legacy=legacy):
                    models, _, inventory, _ = self.fixture(provisioning=True)
                    if legacy:
                        del models["catalog"][0]["provisioningEnabled"]
                        del models["catalog"][0]["runtimeEnabled"]
                    code, text, _, _, _ = self.run_main(models, inventory, profile=profile)
                    self.assertEqual(code, 0, text)
                    self.assertNotIn("retained_deployment_capacity_", text)

    def test_retained_actual_allocation_with_outside_and_reserves(self):
        for runtime in (False, True):
            models, policy, inventory, quotas = self.fixture(runtime=runtime)
            result = production.check_live_pools(policy, inventory, quotas, include_anthropic=False)[0]
            self.assertEqual(result["currentCatalogAllocation"], 100)
            self.assertEqual(result["proposedCatalogAllocation"], 100)
            self.assertEqual(result["outsideCatalogOrUnattributedAllocation"], 40)
            self.assertEqual(sum(result["reserve"].values()), 90)
            for rows in quotas.values():
                rows[0]["limit"] = 190
            with self.assertRaisesRegex(evidence.EvidenceError, "production_pool_reserve_exceeded"):
                production.check_live_pools(policy, inventory, quotas, include_anthropic=False)
            for limit in (300, 230, 229, 190):
                with self.subTest(runtime=runtime, limit=limit):
                    code, text, _, _, _ = self.run_main(models, inventory, limit=limit)
                    self.assertEqual(code, 1, text)
                    self.assertIn("retained_deployment_capacity_mismatch", text)
                    self.assertNotIn("selected 60", text)
                    if limit < 230:
                        self.assertIn("production_pool_reserve_exceeded", text)
                    else:
                        self.assertIn("selected 100", text)

    def test_provisioned_runtime_off_and_legacy_keep_desired_selection(self):
        for legacy in (False, True):
            models, _, inventory, _ = self.fixture(provisioning=True)
            if legacy:
                del models["catalog"][0]["runtimeEnabled"]
                del models["catalog"][0]["provisioningEnabled"]
            code, text, _, offers, _ = self.run_main(models, inventory, limit=190)
            self.assertEqual(code, 0, text)
            self.assertIn("selected 60", text)
            self.assertEqual(len(offers), 2)

    def test_absence_requires_terminal_inventory_and_only_both_flags_off(self):
        models, policy, _, quotas = self.fixture()
        with self.assertRaisesRegex(evidence.EvidenceError, "optional_deployment_inventory_unknown"):
            production.check_live_pools(policy, {}, quotas, include_anthropic=False)
        self.assertEqual(production.check_live_pools(
            policy, {}, {}, include_anthropic=False, complete_regions=frozenset(models["regions"]),
        ), [])
        for runtime, provisioning in ((False, False), (True, False), (False, True)):
            models, _, _, _ = self.fixture(runtime, provisioning)
            code, text, calls, offers, quota_reads = self.run_main(models, {})
            self.assertEqual(code, 0 if not (runtime or provisioning) else 1, text)
            self.assertEqual(sum(c[:4] == ("cognitiveservices", "account", "deployment", "list") for c in calls), 2)
            if not (runtime or provisioning):
                self.assertEqual(offers, [])
                self.assertEqual(quota_reads, [])

    def test_optional_missing_production_metadata_is_lazy_but_retained_is_not(self):
        models, _, inventory, _ = self.fixture()
        for d in models["catalog"][0]["deployments"]:
            del d["production"]
        self.assertIsNotNone(production.parse_policy(models, required=True, include_anthropic=False))
        code, text, _, offers, quota_reads = self.run_main(models, {})
        self.assertEqual(code, 0, text)
        self.assertFalse(offers or quota_reads)
        code, text, _, offers, quota_reads = self.run_main(models, inventory)
        self.assertEqual(code, 1, text)
        self.assertIn("retained_production_capacity_not_configured", text)
        self.assertFalse(offers or quota_reads)
        models["catalog"][0]["runtimeEnabled"] = True
        with self.assertRaisesRegex(evidence.EvidenceError, "production_deployment_not_configured"):
            production.parse_policy(models, required=True, include_anthropic=False)

    def test_new_staged_identity_does_not_request_offering_or_capacity(self):
        models, _, inventory, _ = self.fixture(provisioning=True)
        staged = copy.deepcopy(models["catalog"][0])
        staged.update(name="staged-model", runtimeEnabled=False, provisioningEnabled=False)
        for d in staged["deployments"]:
            del d["production"]
        models["catalog"].append(staged)
        code, text, _, offers, _ = self.run_main(models, inventory)
        self.assertEqual(code, 0, text)
        self.assertIn("Checking 1 deployments in eastus2", text)
        self.assertEqual(len(offers), 2)
        requirements = preflight.catalog_requirements(models, capacity_profile="production", include_anthropic=False)
        self.assertIsNone(requirements["eastus2"][1]["capacity"])

    def test_retained_exact_evidence_and_integer_bounds_refuse(self):
        mutations = {
            "id": ("wrong", None),
            "accountName": ("other-account", None),
            "deploymentName": ("unknown", None),
            "region": ("other-region", None),
            "format": ("Other", None),
            "modelName": ("unknown", None),
            "version": ("unreviewed", 1, None),
            "sku": ("Standard", None),
            "provisioningState": ("Failed", None),
            "versionUpgradeOption": ("OnceNewDefaultVersionAvailable", None),
            "capacity": (None, True, 0, -1, 50.0, "50", 101, 29),
        }
        for field, values in mutations.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    _, policy, inventory, quotas = self.fixture()
                    next(iter(inventory.values()))[field] = value
                    with self.assertRaises(evidence.EvidenceError):
                        production.check_live_pools(policy, inventory, quotas, include_anthropic=False)

    def test_retained_boolean_capacity_is_not_integer_one(self):
        models, _, inventory, quotas = self.fixture()
        for d in models["catalog"][0]["deployments"]:
            d["capacity"] = 1
            d["production"]["criticalMinimum"] = int(d["production"]["critical"])
        policy = production.parse_policy(models, required=True, include_anthropic=False)
        assert policy is not None
        row = next(iter(inventory.values()))
        row["capacity"] = 1
        self.assertTrue(production.check_live_pools(policy, inventory, quotas, include_anthropic=False))
        row["capacity"] = True
        with self.assertRaisesRegex(evidence.EvidenceError, "invalid_production_integer"):
            production.check_live_pools(policy, inventory, quotas, include_anthropic=False)

    def test_failed_partial_continued_duplicate_inventory_never_establishes_absence(self):
        models, _, inventory, _ = self.fixture()
        for profile in ("baseline", "production", "maximum"):
            for malformed in ("continued-accounts", "duplicate-account", "continued-deployments", "duplicate-deployment"):
                with self.subTest(profile=profile, malformed=malformed):
                    code, text, _, _, _ = self.run_main(models, inventory, malformed=malformed, profile=profile)
                    self.assertNotEqual(code, 0, text)
            for current in ({}, inventory):
                code, text, _, offers, quota_reads = self.run_main(
                    models, current, failure="mf-demo-example-swedencentral-abcdefghijklm", profile=profile,
                )
                self.assertNotEqual(code, 0, text)
                if profile == "production":
                    self.assertIn("production_inventory_unknown", text)
                self.assertFalse(offers or quota_reads)

    def test_inventory_failures_preserve_region_and_redacted_command_context(self):
        models, _, inventory, _ = self.fixture()
        for profile in ("baseline", "maximum", "production"):
            with self.subTest(profile=profile):
                code, text, _, offers, quota_reads = self.run_main(
                    models, inventory, profile=profile,
                    failure="mf-demo-example-eastus2-abcdefghijklm",
                    failure_detail=(
                        'synthetic failure\nAuthorization: Bearer private-bearer '
                        '"api_key": "private-key" password=private-password '
                        "https://example.invalid/?sig=private-signature "
                        + "a" * 40
                    ),
                )
                self.assertNotEqual(code, 0)
                self.assertIn("deployment inventory in eastus2", text)
                self.assertIn("synthetic failure", text)
                self.assertIn("could not list model deployments", text)
                for private in (
                    "private-bearer", "private-key", "private-password", "private-signature", "a" * 40,
                ):
                    self.assertNotIn(private, text)
                self.assertFalse(offers or quota_reads)

    def test_inventory_diagnostic_is_bounded_and_control_safe(self):
        detail = preflight._inventory_diagnostic(SystemExit(
            "synthetic failure\x1b\nabcdefgh.ijklmnop.qrstuvwx " + "word " * 2000,
        ))
        self.assertIn("synthetic failure", detail)
        self.assertNotIn("abcdefgh.ijklmnop.qrstuvwx", detail)
        self.assertLessEqual(len(detail), 512)
        self.assertNotRegex(detail, r"[\x00-\x1f\x7f-\x9f]")

    def test_os_inventory_failure_keeps_typed_reason_without_exception_payload(self):
        models, _, _, _ = self.fixture()
        observed, diagnostics = {}, []
        with patch.object(
            preflight, "_regional_deployment_inventory", side_effect=OSError("private OS payload"),
        ):
            inventory, _ = preflight.existing_deployment_inventory(
                models, resource_group="rg-demo-example", environment_name="example",
                region_reads=observed, diagnostics=diagnostics,
                read=lambda *args: subprocess.CompletedProcess(args, 0, stdout=(
                    "true" if args[:2] == ("group", "exists") else "[]"
                ), stderr=""),
            )
        self.assertEqual(inventory, {})
        self.assertEqual(len(diagnostics), len(models["regions"]))
        self.assertTrue(all(row.status == "unavailable" for row in observed.values()))
        self.assertIn("deployment inventory in eastus2: OSError", diagnostics[0])
        self.assertNotIn("private OS payload", "".join(diagnostics))

    def test_malformed_flags_fail_before_any_reads(self):
        for field in ("runtimeEnabled", "provisioningEnabled"):
            for value in (None, "false", 0, 1, []):
                with self.subTest(field=field, value=value):
                    models, _, _, _ = self.fixture()
                    models["catalog"][0][field] = value
                    for profile in ("baseline", "production", "maximum"):
                        code, text, calls, offers, quota_reads = self.run_main(models, {}, profile=profile)
                        self.assertEqual(code, 1, text)
                        self.assertIn("invalid_production_model_intent", text)
                        self.assertFalse(calls or offers or quota_reads)


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
        extra.update(
            name="partner-model", format="Anthropic", api="anthropic",
            deploymentTarget="external-claude", anthropicThinking="disabled",
            samplingSupported=False, reasoningEffort=["low", "medium", "high"],
        )
        extra["deployments"] = [d for d in extra["deployments"] if d["region"] == "eastus2"]
        for deployment in extra["deployments"]:
            del deployment["production"]
            deployment.pop("maxCapacity", None)
            deployment.pop("maxCapacityPool", None)
        models["catalog"].append(extra)
        selected = preflight.catalog_requirements(models, capacity_profile="production", include_anthropic=False)
        self.assertEqual(sum(map(len, selected.values())), 2)
        with self.assertRaisesRegex(evidence.EvidenceError, "external_target_production_policy_unsupported"):
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
