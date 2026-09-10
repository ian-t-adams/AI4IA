"""Exercise the actual bounded collector -> offline recommendation seam, without Azure."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import socket
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.tests._loader import load_script
from scripts.tests._production_fixture import SUBSCRIPTION, production_document
from scripts.tests.test_capacity_evidence import NOW, Fixture, capacity, write_json

import _capacity_recommendations as recommendations

ROOT = Path(__file__).resolve().parents[2]
cli = load_script("recommend_model_capacity", ROOT / "scripts" / "recommend-model-capacity.py")


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fixture = self.make_fixture()
        self.report_path = self.directory / "report.json"

    def make_fixture(self, document=None, days=1):
        fixture = Fixture(self.directory, document or production_document(), days=days)
        for key, response in fixture.responses.items():
            if key.startswith("metrics:"):
                for metric in response["value"]:
                    for series in metric["timeseries"]:
                        for point in series["data"]:
                            point["total"] = 200
        return fixture

    def report(self, fixture=None, *, assertions=True):
        fixture = fixture or self.fixture
        policy = recommendations.load_policy(fixture.catalog_path)
        scopes = [p.assertion["scope"] for p in policy.pools]
        return fixture.report(fixture.evidence(scopes) if assertions else None)

    def run_report(self, report, *, now=NOW):
        write_json(self.report_path, report)
        policy = recommendations.load_policy(self.fixture.catalog_path)
        snapshot = recommendations.load_snapshot(self.report_path, policy, now)
        return recommendations.recommend(policy, snapshot, now)

    def test_golden_actual_report_proposes_without_writes_or_azure(self):
        report = self.report()
        self.assertEqual(report["status"], "complete")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with patch.object(capacity.AzureReader, "read", side_effect=AssertionError("Azure forbidden")), \
             patch.object(subprocess, "run", side_effect=AssertionError("process forbidden")), \
             patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            plan = self.run_report(report)
        self.assertEqual(plan["status"], "complete")
        self.assertEqual(plan["azureCalls"], 0)
        self.assertEqual(plan["writes"], "none")
        pool = plan["pools"][0]
        self.assertEqual([r["recommendedCapacity"] for r in pool["deployments"]], [30, 20])
        self.assertEqual([r["action"] for r in pool["deployments"]], ["decrease", "decrease"])
        self.assertEqual(pool["budget"]["proposedCatalogAllocation"], 50)
        self.assertEqual(pool["budget"]["outsideCatalogOrUnattributedAllocation"], 40)
        self.assertEqual(pool["budget"]["headroomAfter"], 210)
        self.assertEqual(pool["budget"]["unreservedHeadroomAfter"], 120)
        self.assertEqual(pool["budget"]["authority"], "operator_asserted")
        for name, body in before.items():
            self.assertEqual((self.directory / name).read_bytes(), body)
        self.assertEqual(set(p.name for p in self.directory.iterdir()) - before.keys(), {"report.json"})

    def test_policy_integer_rounding_critical_floor_and_ceiling(self):
        document = production_document()
        document["productionCapacityPolicy"]["pools"][0]["usage"]["countPerCapacityHour"] = 7
        self.fixture = self.make_fixture(document)
        plan = self.run_report(self.report())
        self.assertEqual([r["recommendedCapacity"] for r in plan["pools"][0]["deployments"]], [30, 29])
        document["catalog"][0]["deployments"][1]["production"]["ceiling"] = 28
        self.fixture = self.make_fixture(document)
        refused = self.run_report(self.report())
        self.assertEqual(refused["status"], "partial")
        self.assertIn("observed_demand_above_production_ceiling", refused["pools"][0]["codes"])
        self.assertIsNone(refused["pools"][0]["budget"])

    def test_exact_reserve_boundary_and_one_unit_over_control(self):
        for reserve, expected in ((140, "complete"), (141, "partial")):
            with self.subTest(reserve=reserve):
                document = production_document()
                document["productionCapacityPolicy"]["pools"][0]["reserve"]["otherWorkloads"] = reserve
                self.fixture = self.make_fixture(document)
                plan = self.run_report(self.report())
                self.assertEqual(plan["status"], expected)
                pool = plan["pools"][0]
                if expected == "complete":
                    self.assertEqual(pool["budget"]["unreservedHeadroomAfter"], 0)
                else:
                    self.assertEqual(pool["codes"], ["production_pool_reserve_exceeded"])
                    self.assertEqual([d["recommendedCapacity"] for d in pool["deployments"]], [50, 50])

    def test_critical_replacement_reserve_is_not_just_a_total(self):
        for replacement, status in ((30, "complete"), (29, "partial")):
            document = production_document()
            document["productionCapacityPolicy"]["pools"][0]["reserve"]["replacement"] = replacement
            self.fixture = self.make_fixture(document)
            plan = self.run_report(self.report())
            self.assertEqual(plan["status"], status)
            if status == "partial":
                self.assertEqual(plan["pools"][0]["codes"], ["production_replacement_reserve_too_small"])

    def test_low_volume_measured_zero_never_means_reduce_or_remove(self):
        self.assertEqual(self.run_report(self.report())["status"], "complete")
        for key, response in self.fixture.responses.items():
            if key.startswith("metrics:"):
                for metric in response["value"]:
                    for series in metric["timeseries"]:
                        for point in series["data"]:
                            point["total"] = 0
        report = self.report()
        self.assertEqual(report["measurementCoverage"]["status"], "complete")
        plan = self.run_report(report)
        self.assertEqual(plan["status"], "partial")
        self.assertEqual(plan["pools"][0]["codes"], ["insufficient_usage_for_sizing"])
        self.assertEqual([r["action"] for r in plan["pools"][0]["deployments"]], ["hold", "hold"])
        self.assertEqual([r["recommendedCapacity"] for r in plan["pools"][0]["deployments"]], [50, 50])

    def test_missing_null_no_series_and_unknown_account_hold(self):
        self.assertEqual(self.run_report(self.report())["status"], "complete")
        cases = ("no_series", "null_sample", "missing_sample", "account", "warning", "metric_source")
        for case in cases:
            with self.subTest(case=case):
                self.fixture = self.make_fixture()
                if case == "no_series":
                    self.fixture.metric()["timeseries"] = []
                elif case == "null_sample":
                    self.fixture.metric()["timeseries"][0]["data"][0]["total"] = None
                elif case == "missing_sample":
                    self.fixture.metric()["timeseries"][0]["data"].pop()
                elif case == "account":
                    self.fixture.responses["accounts"]["value"].pop()
                elif case == "warning":
                    self.fixture.warnings.add("quota:eastus2")
                else:
                    self.fixture.failures["metrics:eastus2"] = "source_timeout"
                plan = self.run_report(self.report())
                self.assertEqual(plan["status"], "partial")
                self.assertIsNone(plan["pools"][0]["budget"])
                self.assertTrue(all(r["action"] == "hold" for r in plan["pools"][0]["deployments"]))

    def test_observation_window_policy_requires_more_than_recent_sample(self):
        document = production_document()
        document["productionCapacityPolicy"]["pools"][0]["usage"]["minimumHours"] = 48
        for days, expected in ((1, "partial"), (2, "complete")):
            self.fixture = self.make_fixture(document, days)
            self.assertEqual(self.run_report(self.report())["status"], expected)

    def test_all_versions_and_global_replicas_count_once(self):
        plan = self.run_report(self.report())
        pool = plan["pools"][0]
        self.assertEqual(pool["pool"]["model"]["versions"], ["1", "2"])
        self.assertEqual(pool["budget"]["counterLimit"], 300)
        self.assertEqual(pool["budget"]["currentCatalogAllocation"], 100)
        self.assertEqual(pool["budget"]["counterCurrentValue"], 140)
        self.assertEqual(pool["budget"]["headroomBefore"], 160)
        report = self.report()
        report["pools"][0]["model"]["versions"] = ["1"]
        with self.assertRaisesRegex(capacity.EvidenceError, "pool_version_membership_mismatch"):
            self.run_report(report)

    def test_regional_and_shared_data_zone_pools_without_a_global_total(self):
        documents = [
            production_document("Standard", ("region:eastus2", "region:swedencentral")),
            production_document("DataZoneStandard", ("data-zone:US",), shared_zone=True),
        ]
        for document in documents:
            with self.subTest(sku=document["catalog"][0]["deployments"][0]["sku"]):
                self.fixture = self.make_fixture(document)
                if len(document["productionCapacityPolicy"]["pools"]) == 2:
                    for region in self.fixture.catalog.regions:
                        self.fixture.responses[f"quota:{region}"]["value"][0]["currentValue"] = 70
                plan = self.run_report(self.report())
                self.assertEqual(plan["status"], "complete")
                self.assertEqual(len(plan["pools"]), len(document["productionCapacityPolicy"]["pools"]))
                self.assertNotIn("headroom", plan)
                self.assertNotIn("totalCapacity", plan)

    def test_partial_pool_does_not_suppress_independent_complete_pool(self):
        self.fixture = self.make_fixture(production_document("Standard", ("region:eastus2", "region:swedencentral")))
        self.fixture.failures["metrics:swedencentral"] = "source_timeout"
        report = self.report()
        self.assertEqual(report["status"], "partial")
        plan = self.run_report(report)
        self.assertEqual(plan["status"], "partial")
        self.assertEqual([p["status"] for p in plan["pools"]], ["recommended", "unknown"])

    def test_unlike_units_remain_separate_under_explicit_regional_assertions(self):
        document = production_document("Standard", ("region:eastus2", "region:swedencentral"))
        document["productionCapacityPolicy"]["pools"][1]["pool"]["unit"] = "Tokens"
        self.fixture = self.make_fixture(document)
        assertions = self.fixture.evidence(["region:eastus2", "region:swedencentral"])
        assertions["pools"][1]["unit"] = "Tokens"
        self.fixture.responses["quota:swedencentral"]["value"][0]["unit"] = "Tokens"
        plan = self.run_report(self.fixture.report(assertions))
        self.assertEqual(plan["status"], "complete")
        self.assertEqual([p["budget"]["unit"] for p in plan["pools"]], ["Count", "Tokens"])
        self.assertNotIn("totalCapacity", plan)
        self.assertNotIn("headroom", plan)

    def test_uncatalogued_state_is_not_invented_from_an_aggregate_report(self):
        good = self.run_report(self.report())
        self.assertEqual(good["status"], "complete")
        row = copy.deepcopy(self.fixture.responses["deployments:eastus2"]["value"][0])
        row["name"] += "-unmanaged"
        row["id"] = f"{self.fixture.scope.account_path(self.fixture.accounts['eastus2'])}/deployments/{row['name']}"
        row["sku"]["capacity"] = 10
        self.fixture.responses["deployments:eastus2"]["value"].append(row)
        report = self.report()
        self.assertEqual(report["pools"][0]["status"], "consistent")
        self.assertEqual(report["pools"][0]["knownUncataloguedAllocation"], 10)
        plan = self.run_report(report)
        self.assertEqual(plan["status"], "partial")
        self.assertIn("unsettled_or_unreviewed_pool_deployment", plan["pools"][0]["evidenceCodes"])
        self.assertIsNone(plan["pools"][0]["budget"])

    def test_unasserted_units_scope_and_tampered_rollup_refuse(self):
        self.assertEqual(self.run_report(self.report())["status"], "complete")
        unasserted = self.run_report(self.report(assertions=False))
        self.assertEqual(unasserted["pools"][0]["codes"], ["pool_assertion_not_established"])
        for field, value in (("unit", "Tokens"), ("counter", "different-counter")):
            report = self.report()
            report["pools"][0][field] = value
            plan = self.run_report(report)
            self.assertEqual(plan["status"], "partial")
        report = self.report()
        report["pools"][0]["headroom"] += 1
        self.assertEqual(self.run_report(report)["pools"][0]["codes"], ["pool_rollup_mismatch"])
        report = self.report()
        report["pools"].append(copy.deepcopy(report["pools"][0]))
        with self.assertRaisesRegex(capacity.EvidenceError, "overlapping_pool_membership"):
            self.run_report(report)

    def test_wrong_scope_catalog_and_stale_reports_are_rejected(self):
        good = self.report()
        for path, replacement in (
            (("scope", "subscriptionFingerprint"), "0" * 64),
            (("scope", "environment"), "different"),
            (("scope", "resourceGroup"), "rg-other"),
            (("catalog", "sha256"), "0" * 64),
        ):
            report = copy.deepcopy(good)
            report[path[0]][path[1]] = replacement
            with self.subTest(path=path), self.assertRaisesRegex(capacity.EvidenceError, "report_catalog_or_scope_mismatch"):
                self.run_report(report)
        with self.assertRaisesRegex(capacity.EvidenceError, "stale_capacity_report"):
            self.run_report(good, now=NOW + timedelta(hours=24, seconds=1))
        report = copy.deepcopy(good)
        report["poolEvidence"]["observedAt"] = capacity.utc_text(NOW - timedelta(hours=25))
        with self.assertRaisesRegex(capacity.EvidenceError, "stale_pool_evidence"):
            self.run_report(report)
        report = copy.deepcopy(good)
        report["sources"][0]["finishedAt"] = capacity.utc_text(NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(capacity.EvidenceError, "report_source_time_mismatch"):
            self.run_report(report)
        document = production_document()
        document["productionCapacityPolicy"]["pools"][0]["usage"]["minimumTotal"] += 1
        write_json(self.fixture.catalog_path, document)
        with self.assertRaisesRegex(capacity.EvidenceError, "report_catalog_or_scope_mismatch"):
            self.run_report(good)

    def test_missing_unattributed_and_counter_values_are_not_zero(self):
        for value in (None, -1, 1.5, True, 2**53):
            report = self.report()
            report["quotaCounters"][0]["observations"][0]["currentValue"] = value
            with self.subTest(value=value), self.assertRaises(capacity.EvidenceError):
                self.run_report(report)
        report = self.report()
        report["quotaCounters"][0]["observations"][1]["currentValue"] += 1
        self.assertEqual(self.run_report(report)["status"], "partial")
        self.fixture.responses["quota:eastus2"]["value"][0]["currentValue"] = 99
        self.fixture.responses["quota:swedencentral"]["value"][0]["currentValue"] = 99
        self.assertEqual(self.run_report(self.report())["status"], "partial")

    def test_increases_do_not_spend_unapplied_reductions_or_add_platform_replicas(self):
        document = production_document()
        document["productionCapacityPolicy"]["pools"][0]["usage"]["countPerCapacityHour"] = 3
        document["productionCapacityPolicy"]["pools"][0]["reserve"]["replacement"] = 70
        self.fixture = self.make_fixture(document)
        allowed = self.run_report(self.report())
        self.assertEqual(allowed["status"], "complete")
        self.assertEqual([r["recommendedCapacity"] for r in allowed["pools"][0]["deployments"]], [67, 67])
        for key, response in self.fixture.responses.items():
            if key.startswith("availability:"):
                for row in response["value"]:
                    row["properties"]["availableCapacity"] = 33
        denied = self.run_report(self.report())
        self.assertEqual(denied["status"], "partial")
        self.assertEqual(denied["pools"][0]["codes"], ["platform_headroom_insufficient"])

    def test_cli_has_no_output_or_apply_mode_and_is_bounded(self):
        report = self.report()
        write_json(self.report_path, report)
        arguments = [
            "--report", str(self.report_path), "--subscription", SUBSCRIPTION,
            "--resource-group", "rg-demo-example", "--environment-name", "example", "--format", "json",
        ]
        with patch.object(cli, "CATALOG", self.fixture.catalog_path), \
             patch.object(cli, "datetime") as clock, \
             patch.object(subprocess, "run", side_effect=AssertionError("no process")), \
             patch.object(capacity.AzureReader, "read", side_effect=AssertionError("no Azure")):
            clock.now.return_value = NOW
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "complete")
            for flag in ("--apply", "--output"):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
                    cli.main([*arguments, flag])
                self.assertEqual(exit_info.exception.code, 2)
            with patch.object(capacity, "MAX_REPORT_BYTES", len(self.report_path.read_bytes()) - 1):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(arguments), 2)
                self.assertEqual(json.loads(output.getvalue())["error"], "local_evidence_too_large")
            self.assertEqual({p.name for p in self.directory.iterdir()}, {"models.json", "pool-evidence.json", "report.json"})

    def test_strict_json_and_escaped_output_limits(self):
        policy = recommendations.load_policy(self.fixture.catalog_path)
        for body in (b'{"schemaVersion":1,"schemaVersion":1}', b'{"schemaVersion":NaN}', b"[" * 1100):
            self.report_path.write_bytes(body)
            with self.assertRaises(capacity.EvidenceError):
                recommendations.load_snapshot(self.report_path, policy, NOW)
        plan = self.run_report(self.report())
        rendered = recommendations.render(plan, "json")
        self.assertEqual(json.loads(rendered)["writes"], "none")
        with patch.object(capacity, "MAX_REPORT_BYTES", len(rendered.encode()) - 1), \
             self.assertRaisesRegex(capacity.EvidenceError, "report_too_large"):
            recommendations.render(plan, "json")
        self.report_path.write_bytes(b" " * (capacity.MAX_REPORT_BYTES + 1))
        with self.assertRaisesRegex(capacity.EvidenceError, "local_evidence_too_large"):
            recommendations.load_snapshot(self.report_path, policy, NOW)
        report = self.report()
        report["sources"] *= capacity.MAX_CALLS
        with self.assertRaisesRegex(capacity.EvidenceError, "row_limit_exceeded"):
            self.run_report(report)


if __name__ == "__main__":
    unittest.main()
