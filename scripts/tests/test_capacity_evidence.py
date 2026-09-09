"""Read-only capacity observations must keep missing evidence distinct from zero."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import _capacity_evidence as capacity

cli = load_script("report_model_capacity", ROOT / "scripts" / "report-model-capacity.py")
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
OTHER_SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
SECRET = "PRIVATE-UPSTREAM-CONTENT"


def catalog_document(sku: str = "GlobalStandard") -> dict:
    return {
        "naming": {
            "foundryToken": "demo", "subscriptionToken": "example",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {"GlobalStandard": "glbl", "Standard": "std", "DataZoneStandard": "dz"},
        },
        "regions": {"eastus2": {"dataZone": "US"}, "swedencentral": {"dataZone": "EU"}},
        "catalog": [{
            "name": "model-a", "format": "OpenAI",
            "deployments": [
                {"region": region, "sku": sku, "version": "1", "capacity": 10,
                 "maxCapacity": 900, "maxCapacityPool": "global"}
                for region in ("eastus2", "swedencentral")
            ],
        }],
    }


def write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


class Fixture:
    def __init__(self, directory: Path, document: dict | None = None, days: int = 1):
        self.directory = directory
        self.catalog_path = directory / "models.json"
        write_json(self.catalog_path, document if document is not None else catalog_document())
        self.catalog = capacity.load_catalog(self.catalog_path)
        self.scope = capacity.Scope(SUBSCRIPTION, "rg-demo-example", "example", self.catalog)
        self.window = capacity.Window.create(days, None, NOW)
        self.accounts = {
            region: f"mf-demo-example-{region}-abcdefghijklm" for region in self.catalog.regions
        }
        self.calls = []
        self.responses = {}
        self.failures = {}
        self.warnings = set()
        self.responses["group"] = {
            "id": self.scope.group_path, "name": self.scope.resource_group,
            "env": "example", "azdEnv": "example", "managedBy": "azd-bicep",
        }
        self.responses["accounts"] = {"value": [
            {
                "id": self.scope.account_path(account), "name": account, "kind": "AIServices",
                "location": region, "env": "example", "azdEnv": "example", "managedBy": "azd-bicep",
            }
            for region, account in self.accounts.items()
        ]}
        for region, account in self.accounts.items():
            deployments = [d for d in self.catalog.deployments if d.region == region]
            self.responses[f"deployments:{region}"] = {"value": [
                {
                    "id": f"{self.scope.account_path(account)}/deployments/{d.name}", "name": d.name,
                    "sku": {"name": d.model.sku, "capacity": 50},
                    "properties": {
                        "model": {"format": d.model.format, "name": d.model.name, "version": d.model.version},
                        "provisioningState": "Succeeded",
                    },
                }
                for d in deployments
            ]}
            counters = {f"AIServices.{d.model.sku}.{d.model.name}" for d in self.catalog.deployments}
            self.responses[f"quota:{region}"] = {"value": [
                {"name": {"value": counter}, "currentValue": 140, "limit": 300, "unit": "Count"}
                for counter in sorted(counters)
            ]}
            self.responses[f"definitions:{region}"] = {"value": [
                {
                    "name": {"value": metric}, "unit": "Count",
                    "dimensions": [{"value": dimension} for dimension in capacity.DIMENSIONS],
                    "supportedAggregationTypes": ["Total"], "primaryAggregationType": "Total",
                    "metricAvailabilities": [{"timeGrain": "PT1H"}],
                }
                for metric in capacity.METRICS
            ]}
            self.responses[f"metrics:{region}"] = {
                "namespace": capacity.NAMESPACE, "resourceregion": region,
                "timespan": self.window.timespan, "interval": "PT1H",
                "value": [
                    {
                        "id": f"{self.scope.account_path(account)}/providers/Microsoft.Insights/metrics/{metric}",
                        "name": {"value": metric}, "unit": "Count", "errorCode": "Success",
                        "hasErrorMessage": False,
                        "timeseries": [self.series(d, region) for d in deployments],
                    }
                    for metric in capacity.METRICS
                ],
            }
        for d in self.catalog.deployments:
            model = d.model
            key = capacity.availability_source_id((model.format, model.name, model.version))
            skus = {
                item.model.sku for item in self.catalog.deployments
                if (item.model.format, item.model.name, item.model.version) == (model.format, model.name, model.version)
            }
            self.responses[key] = {"value": [
                {
                    "location": region,
                    "properties": {
                        "model": {"format": model.format, "name": model.name, "version": model.version},
                        "skuName": sku, "availableCapacity": 150,
                    },
                }
                for region in self.catalog.regions for sku in sorted(skus)
            ]}

    def series(self, deployment, inference_region: str) -> dict:
        values = (deployment.name, deployment.model.name, deployment.model.version, inference_region)
        return {
            "dimensionCount": 4,
            "metadatavalues": [
                {"name": {"value": dimension}, "value": value}
                for dimension, value in zip(capacity.DIMENSIONS, values)
            ],
            "data": [
                {"timeStamp": capacity.utc_text(self.window.start + timedelta(hours=hour)), "total": 0}
                for hour in range(self.window.hours)
            ],
        }

    def evidence(self, scopes: list[str] | None = None) -> dict:
        pools = []
        for scope_name in scopes or ["global"]:
            regions = capacity.scope_regions(scope_name, self.catalog)
            models = {(d.model.format, d.model.name, d.model.sku) for d in self.catalog.deployments if d.region in regions}
            for format_name, name, sku in sorted(models):
                versions = sorted({
                    d.model.version for d in self.catalog.deployments
                    if d.region in regions and (d.model.format, d.model.name, d.model.sku) == (format_name, name, sku)
                })
                pools.append({
                    "scope": scope_name, "counter": f"AIServices.{sku}.{name}", "unit": "Count",
                    "regions": sorted(regions),
                    "model": {"format": format_name, "name": name, "sku": sku, "versions": versions},
                    "capacityUnitsPerCounterUnit": 1,
                })
        return {
            "schemaVersion": 1, "subscriptionId": SUBSCRIPTION,
            "observedAt": capacity.utc_text(NOW - timedelta(hours=1)),
            "reference": "review-2026-09-09-001", "pools": pools,
        }

    def runner(self, command: list[str], timeout: float, limit: int):
        self.calls.append(command)
        if command[0] != "mock-az" or command[1:4] != ["rest", "--method", "GET"]:
            raise AssertionError("unexpected command")
        if command[command.index("--subscription") + 1] != SUBSCRIPTION:
            raise AssertionError("unscoped subscription")
        if not 0 < timeout <= capacity.SOURCE_SECONDS or not 0 < limit <= capacity.MAX_RESPONSE_BYTES:
            raise AssertionError("unbounded transport")
        uri = urlsplit(command[command.index("--url") + 1])
        if uri.scheme != "https" or uri.netloc != "management.azure.com":
            raise AssertionError("unapproved host")
        if not uri.path.startswith(f"/subscriptions/{SUBSCRIPTION}/"):
            raise AssertionError("wrong URL subscription")
        query = parse_qs(uri.query)
        path = uri.path
        if path == self.scope.group_path:
            key = "group"
        elif path == f"{self.scope.group_path}/providers/{capacity.NAMESPACE}":
            key = "accounts"
        elif path.endswith("/usages"):
            key = "quota:" + path.split("/")[-2]
        elif path.endswith("/modelCapacities"):
            key = capacity.availability_source_id(tuple(query[name][0] for name in ("modelFormat", "modelName", "modelVersion")))
        else:
            matched = [(r, a) for r, a in self.accounts.items() if path.startswith(self.scope.account_path(a) + "/")]
            if len(matched) != 1:
                raise AssertionError("unexpected account read")
            region, _account = matched[0]
            suffix = path.rsplit("/", 1)[-1]
            operation = {"deployments": "deployments", "metricDefinitions": "definitions", "metrics": "metrics"}.get(suffix)
            if operation is None:
                raise AssertionError("unexpected Azure read")
            key = f"{operation}:{region}"
        if key in self.failures:
            raise capacity.EvidenceError(self.failures[key])
        document = copy.deepcopy(self.responses[key])
        if key.startswith("metrics:"):
            requested = query["metricnames"][0].split(",")
            document["value"] = [row for row in document["value"] if row["name"]["value"] in requested]
        body = json.dumps(document).encode()
        return capacity.ProcessResult(body, key in self.warnings)

    def report(self, evidence: dict | None = None, reader=None) -> dict:
        pool_path = None
        if evidence is not None:
            pool_path = self.directory / "pool-evidence.json"
            write_json(pool_path, evidence)
        with patch.object(capacity, "az_command", return_value=["mock-az"]):
            reader = reader or capacity.AzureReader(self.scope, runner=self.runner)
            return capacity.collect(self.scope, self.window, reader, pool_path, now=lambda: NOW)

    def metric(self, name="ModelRequests", region="eastus2") -> dict:
        return next(row for row in self.responses[f"metrics:{region}"]["value"] if row["name"]["value"] == name)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fixture = Fixture(self.directory)

    def first_usage(self, report, metric="ModelRequests"):
        return report["deployments"][0]["usage"][metric]

    def source(self, report, source_id):
        return next(row for row in report["sources"] if row["id"] == source_id)

    def test_actual_collector_measures_zero_without_asserting_default_pools(self):
        report = self.fixture.report()
        self.assertTrue(self.fixture.calls)
        self.assertEqual(report["measurementCoverage"]["status"], "complete")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["poolCoverage"]["status"], "unknown")
        self.assertEqual(report["pools"], [])
        self.assertEqual(report["recommendations"], [])
        self.assertEqual(self.first_usage(report)["total"], 0)
        self.assertEqual(self.first_usage(report)["samples"], 24)
        self.assertEqual(self.first_usage(report)["zeroSamples"], 24)
        self.assertEqual(report["deployments"][0]["catalog"]["declaredPool"], "global")
        self.assertEqual(report["deployments"][0]["catalog"]["poolAuthority"], "unverified_catalog_declaration")
        self.assertIsNone(report["quotaCounters"][0]["headroom"])
        self.assertEqual(report["cost"]["status"], "unknown")

    def test_shared_counter_is_counted_once_and_preserves_external_allocation(self):
        report = self.fixture.report(self.fixture.evidence())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["quotaCounters"]), 1)
        self.assertEqual(len(report["quotaCounters"][0]["observations"]), 2)
        self.assertEqual(len(report["pools"]), 1)
        pool = report["pools"][0]
        self.assertEqual(pool["counterCurrentValue"], 140)
        self.assertEqual(pool["catalogAllocation"], 100)
        self.assertEqual(pool["outsideCatalogOrUnattributedAllocation"], 40)
        self.assertEqual(pool["headroom"], 160)
        self.assertIsNone(pool["deployableHeadroom"])
        self.assertEqual(pool["authority"], "operator_asserted")
        self.assertEqual(report["recommendations"], [])

    def test_shared_data_zone_and_independent_regions_are_not_global_totals(self):
        document = catalog_document("DataZoneStandard")
        document["regions"]["swedencentral"]["dataZone"] = "US"
        fixture = Fixture(self.directory, document)
        report = fixture.report(fixture.evidence(["data-zone:US"]))
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["pools"]), 1)
        self.assertEqual(report["pools"][0]["catalogAllocation"], 100)
        self.assertEqual(report["pools"][0]["headroom"], 160)
        fixture = Fixture(self.directory, catalog_document("Standard"))
        report = fixture.report(fixture.evidence(["region:eastus2", "region:swedencentral"]))
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["pools"]), 2)
        self.assertEqual([p["catalogAllocation"] for p in report["pools"]], [50, 50])
        self.assertNotIn("totalCapacity", report)
        self.assertNotIn("totalHeadroom", report)

    def test_unlike_counters_and_units_are_not_added(self):
        document = catalog_document()
        other = copy.deepcopy(document["catalog"][0])
        other["name"] = "model-b"
        document["catalog"].append(other)
        fixture = Fixture(self.directory, document)
        for region in fixture.catalog.regions:
            fixture.responses[f"quota:{region}"]["value"][1]["unit"] = "Seconds"
        evidence = fixture.evidence()
        evidence["pools"][1]["unit"] = "Seconds"
        report = fixture.report(evidence)
        self.assertEqual(report["status"], "complete")
        self.assertEqual({p["unit"] for p in report["pools"]}, {"Count", "Seconds"})
        self.assertEqual(len(report["quotaCounters"]), 2)
        self.assertNotIn("total", report)

    def test_multi_version_counter_is_one_pool_and_cannot_be_split(self):
        document = catalog_document()
        document["catalog"][0]["deployments"][1]["version"] = "2"
        fixture = Fixture(self.directory, document)
        good = fixture.evidence()
        report = fixture.report(good)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["pools"][0]["model"]["versions"], ["1", "2"])
        self.assertEqual(report["pools"][0]["catalogAllocation"], 100)
        bad = copy.deepcopy(good)
        bad["pools"] = [copy.deepcopy(good["pools"][0]), copy.deepcopy(good["pools"][0])]
        split = fixture.report(bad)
        self.assertEqual(split["poolEvidence"]["error"], "overlapping_pool_membership")
        self.assertEqual(split["pools"], [])
        bad["pools"][0]["model"]["versions"] = ["1"]
        bad["pools"][1]["model"]["versions"] = ["2"]
        split = fixture.report(bad)
        self.assertEqual(split["poolEvidence"]["error"], "pool_version_membership_mismatch")
        self.assertEqual(split["pools"], [])

    def test_scope_overlap_cannot_double_count_global_regional_or_data_zone(self):
        fixture = self.fixture
        good = fixture.evidence()
        self.assertEqual(fixture.report(good)["status"], "complete")
        for scope_name in ("region:eastus2", "data-zone:US"):
            with self.subTest(scope=scope_name):
                bad = fixture.evidence(["global", scope_name])
                report = fixture.report(bad)
                self.assertEqual(report["poolEvidence"]["error"], "overlapping_pool_membership")
                self.assertEqual(report["pools"], [])
                self.assertEqual(report["recommendations"], [])

    def test_counter_overlap_is_rejected_even_with_different_model_memberships(self):
        document = catalog_document()
        other = copy.deepcopy(document["catalog"][0])
        other["name"] = "model-b"
        document["catalog"].append(other)
        fixture = Fixture(self.directory, document)
        good = fixture.evidence()
        self.assertEqual(fixture.report(good)["status"], "complete")
        bad = copy.deepcopy(good)
        bad["pools"][1]["counter"] = bad["pools"][0]["counter"]
        report = fixture.report(bad)
        self.assertEqual(report["poolEvidence"]["error"], "overlapping_pool_membership")
        self.assertEqual(report["pools"], [])

    def test_missing_null_error_and_sparse_usage_never_become_idle_recommendations(self):
        original = copy.deepcopy(self.fixture.metric())
        self.assertEqual(self.first_usage(self.fixture.report())["total"], 0)
        cases = {
            "no_series": {**original, "timeseries": []},
            "no_samples": {**original, "timeseries": [{**original["timeseries"][0], "data": []}]},
            "nulls": {
                **original, "timeseries": [{
                    **original["timeseries"][0],
                    "data": [{**point, "total": None} for point in original["timeseries"][0]["data"]],
                }],
            },
            "sparse": {**original, "timeseries": [{**original["timeseries"][0], "data": original["timeseries"][0]["data"][:1]}]},
            "provider_error": {**original, "errorCode": SECRET, "hasErrorMessage": True},
        }
        for name, row in cases.items():
            with self.subTest(case=name):
                self.fixture.responses["metrics:eastus2"]["value"][0] = row
                report = self.fixture.report()
                usage = self.first_usage(report)
                self.assertIsNone(usage["total"])
                self.assertNotEqual(usage["status"], "measured")
                self.assertEqual(report["recommendations"], [])
                self.assertNotIn(SECRET, json.dumps(report))
        self.fixture.responses["metrics:eastus2"]["value"][0] = original
        self.fixture.failures["metrics:eastus2"] = "azure_read_failed"
        report = self.fixture.report()
        self.assertIsNone(self.first_usage(report)["total"])
        self.assertEqual(self.source(report, "metrics:eastus2")["status"], "unavailable")

    def test_partial_totals_keep_samples_without_claiming_window_total(self):
        series = self.fixture.metric()["timeseries"][0]
        series["data"] = series["data"][:2]
        series["data"][0]["total"] = 12
        report = self.fixture.report()
        usage = self.first_usage(report)
        self.assertEqual(usage["status"], "partial")
        self.assertIsNone(usage["total"])
        self.assertEqual(usage["observedTotal"], 12)
        self.assertEqual(usage["samples"], 2)
        self.assertEqual(usage["zeroSamples"], 1)
        self.assertEqual(usage["expectedSamples"], 24)
        self.assertEqual(usage["observedPeakHourlyCount"], 12)

    def test_metric_contract_queries_no_aliases_or_private_dimensions(self):
        report = self.fixture.report()
        for command in self.fixture.calls:
            uri = urlsplit(command[command.index("--url") + 1])
            query = parse_qs(uri.query)
            self.assertNotIn("--apply", command)
            self.assertNotIn("set", command)
            self.assertEqual(command[command.index("--method") + 1], "GET")
            if uri.path.endswith("/metrics"):
                self.assertEqual(query["metricnames"], [",".join(capacity.METRICS)])
                self.assertEqual(query["interval"], ["PT1H"])
                self.assertEqual(query["aggregation"], ["Total"])
                self.assertEqual(query["top"], [str(capacity.MAX_SERIES + 1)])
                self.assertEqual(query["AutoAdjustTimegrain"], ["false"])
                self.assertEqual(query["ValidateDimensions"], ["true"])
                self.assertEqual(query["$filter"], [" and ".join(f"{d} eq '*'" for d in capacity.DIMENSIONS)])
                self.assertEqual(query["timespan"], [self.fixture.window.timespan])
        serialized = json.dumps(report)
        self.assertNotIn(SUBSCRIPTION, serialized)
        self.assertNotIn("/subscriptions/", serialized)
        self.assertIn("subscriptionFingerprint", report["scope"])
        self.assertTrue(any("Voice Live" in reason for reason in report["exclusions"]))

    def test_alias_only_or_unattributable_definitions_do_not_trigger_metric_reads(self):
        self.assertTrue(self.fixture.report()["deployments"][0]["usage"]["ModelRequests"]["samples"])
        self.fixture.calls.clear()
        for definition in self.fixture.responses["definitions:eastus2"]["value"]:
            definition["dimensions"] = [{"value": "ModelName"}]
        self.fixture.responses["definitions:eastus2"]["value"].append({
            "name": {"value": "AzureOpenAIRequests"}, "unit": "Count",
        })
        report = self.fixture.report()
        self.assertNotEqual(self.first_usage(report)["status"], "measured")
        self.assertIn("unsupported_metric_definition", self.source(report, "definitions:eastus2")["codes"])
        self.assertFalse(any(
            urlsplit(c[c.index("--url") + 1]).path == f"{self.fixture.scope.account_path(self.fixture.accounts['eastus2'])}/providers/Microsoft.Insights/metrics"
            for c in self.fixture.calls
        ))
        self.assertEqual(self.first_usage(report, "InputTokens")["observedTotal"], None)

    def test_success_warning_and_pagination_do_not_pass_as_complete(self):
        self.assertEqual(self.fixture.report(self.fixture.evidence())["status"], "complete")
        self.fixture.warnings.add("metrics:eastus2")
        warned = self.fixture.report(self.fixture.evidence())
        self.assertIsNone(self.first_usage(warned)["total"])
        self.assertEqual(self.first_usage(warned)["observedTotal"], 0)
        self.assertIn("azure_cli_warning", self.first_usage(warned)["codes"])
        self.fixture.warnings.clear()
        self.fixture.responses["quota:eastus2"]["nextLink"] = f"https://evil.invalid/{SECRET}"
        paged = self.fixture.report(self.fixture.evidence())
        self.assertEqual(paged["pools"][0]["status"], "unknown")
        self.assertIsNone(paged["pools"][0]["headroom"])
        self.assertNotIn(SECRET, json.dumps(paged))
        self.assertTrue(all("evil.invalid" not in " ".join(c) for c in self.fixture.calls))

    def test_live_and_metric_versions_must_not_be_relabelled_from_catalog(self):
        self.assertEqual(self.fixture.report(self.fixture.evidence())["status"], "complete")
        row = self.fixture.responses["deployments:eastus2"]["value"][0]
        row["properties"]["model"]["version"] = "2"
        for metric in self.fixture.responses["metrics:eastus2"]["value"]:
            metric["timeseries"][0]["metadatavalues"][2]["value"] = "2"
        report = self.fixture.report(self.fixture.evidence())
        deployment = report["deployments"][0]
        self.assertEqual(deployment["inventoryStatus"], "identity_mismatch")
        self.assertEqual(deployment["live"]["model"]["version"], "2")
        self.assertEqual(deployment["catalog"]["model"]["version"], "1")
        self.assertEqual(self.first_usage(report)["status"], "measured")
        self.assertIsNone(report["pools"][0]["headroom"])
        self.fixture.metric()["timeseries"][0]["metadatavalues"][2]["value"] = "1"
        report = self.fixture.report()
        self.assertIn("metric_model_version_mismatch", self.first_usage(report)["codes"])
        self.assertIsNone(self.first_usage(report)["total"])

    def test_unknown_series_are_excluded_and_extra_deployments_are_not_catalog_allocation(self):
        extra = copy.deepcopy(self.fixture.responses["deployments:eastus2"]["value"][0])
        extra["name"] = "not-in-catalog"
        extra["id"] = extra["id"].rsplit("/", 1)[0] + "/not-in-catalog"
        extra["sku"]["capacity"] = 20
        self.fixture.responses["deployments:eastus2"]["value"].append(extra)
        series = copy.deepcopy(self.fixture.metric()["timeseries"][0])
        series["metadatavalues"][0]["value"] = "not-in-catalog"
        series["data"][0]["total"] = 9000
        self.fixture.metric()["timeseries"].append(series)
        report = self.fixture.report(self.fixture.evidence())
        self.assertEqual(report["pools"][0]["catalogAllocation"], 100)
        self.assertEqual(report["pools"][0]["knownUncataloguedAllocation"], 20)
        self.assertEqual(report["pools"][0]["outsideCatalogOrUnattributedAllocation"], 40)
        self.assertEqual(report["excludedMetricSeries"], 1)
        self.assertEqual(len(report["uncataloguedDeployments"]), 1)
        self.assertEqual(self.first_usage(report)["total"], 0)

    def test_uncatalogued_allocations_can_contradict_the_shared_counter(self):
        extra = copy.deepcopy(self.fixture.responses["deployments:eastus2"]["value"][0])
        extra["name"] = "not-in-catalog"
        extra["id"] = extra["id"].rsplit("/", 1)[0] + "/not-in-catalog"
        extra["sku"]["capacity"] = 40
        self.fixture.responses["deployments:eastus2"]["value"].append(extra)
        good = self.fixture.report(self.fixture.evidence())
        self.assertEqual(good["status"], "complete")
        self.assertEqual(good["pools"][0]["knownUncataloguedAllocation"], 40)
        self.assertEqual(good["pools"][0]["headroom"], 160)
        extra["sku"]["capacity"] = 41
        bad = self.fixture.report(self.fixture.evidence())
        self.assertEqual(bad["status"], "partial")
        self.assertIn("contradictory_pool_allocation", bad["pools"][0]["codes"])
        self.assertIsNone(bad["pools"][0]["headroom"])
        self.assertEqual(bad["uncataloguedDeployments"][0]["capacity"], 41)
        extra["sku"]["capacity"] = 40
        extra["properties"]["model"]["version"] = "unknown-version"
        bad = self.fixture.report(self.fixture.evidence())
        self.assertIn("unsettled_or_unreviewed_pool_deployment", bad["pools"][0]["codes"])
        self.assertIsNone(bad["pools"][0]["headroom"])

    def test_duplicate_series_samples_metrics_and_partial_errors_are_not_summed(self):
        original = copy.deepcopy(self.fixture.metric())
        self.assertEqual(self.first_usage(self.fixture.report())["total"], 0)
        self.fixture.metric()["timeseries"].append(copy.deepcopy(original["timeseries"][0]))
        report = self.fixture.report()
        self.assertIsNone(self.first_usage(report)["total"])
        self.assertEqual(self.first_usage(report)["samples"], 24)
        self.assertIn("duplicate_metric_series", self.first_usage(report)["codes"])
        self.fixture.responses["metrics:eastus2"]["value"][0] = copy.deepcopy(original)
        series = self.fixture.metric()["timeseries"][0]
        series["data"][1]["timeStamp"] = series["data"][0]["timeStamp"]
        report = self.fixture.report()
        self.assertIn("duplicate_metric_sample", self.first_usage(report)["codes"])
        self.assertIsNone(self.first_usage(report)["total"])
        self.fixture.responses["metrics:eastus2"]["value"][0] = copy.deepcopy(original)
        self.fixture.responses["metrics:eastus2"]["value"][1] = copy.deepcopy(original)
        report = self.fixture.report()
        self.assertIn("duplicate_metric", self.first_usage(report)["codes"])
        self.assertEqual(self.first_usage(report, "OutputTokens")["total"], 0)

    def test_wildcard_series_limit_has_a_measured_control_at_the_boundary(self):
        original = copy.deepcopy(self.fixture.metric()["timeseries"][0])
        second = copy.deepcopy(original)
        second["metadatavalues"][3]["value"] = "other-region"
        self.fixture.metric()["timeseries"] = [original, second]
        with patch.object(capacity, "MAX_SERIES", 2):
            report = self.fixture.report()
            self.assertEqual(self.first_usage(report)["status"], "measured")
            self.assertEqual(self.first_usage(report)["samples"], 48)
            third = copy.deepcopy(original)
            third["metadatavalues"][3]["value"] = "third-region"
            self.fixture.metric()["timeseries"].append(third)
            report = self.fixture.report()
            self.assertIn("series_limit_exceeded", self.first_usage(report)["codes"])
            self.assertIsNone(self.first_usage(report)["total"])
            self.assertTrue(any("top=3" in c[c.index("--url") + 1] for c in self.fixture.calls))

    def test_numeric_dates_dimensions_and_scope_malformed_values_are_unknown(self):
        original = copy.deepcopy(self.fixture.metric())
        bad_numbers = [True, False, -1, 0.1, "0", float("nan"), float("inf"), 10**400]
        for value in bad_numbers:
            with self.subTest(value=str(value)[:20]):
                self.fixture.responses["metrics:eastus2"]["value"][0] = copy.deepcopy(original)
                self.fixture.metric()["timeseries"][0]["data"][0]["total"] = value
                report = self.fixture.report()
                self.assertIsNone(self.first_usage(report)["total"])
                self.assertNotEqual(self.first_usage(report)["status"], "measured")
        for stamp in ("2026-02-30T00:00:00Z", "2026-09-08", "2026-09-08T11:00:00", "2026-09-08T11:30:00Z", "2020-01-01T00:00:00Z"):
            with self.subTest(stamp=stamp):
                self.fixture.responses["metrics:eastus2"]["value"][0] = copy.deepcopy(original)
                self.fixture.metric()["timeseries"][0]["data"][0]["timeStamp"] = stamp
                self.assertIsNone(self.first_usage(self.fixture.report())["total"])
        self.fixture.responses["metrics:eastus2"]["value"][0] = copy.deepcopy(original)
        self.fixture.metric()["timeseries"][0]["dimensionCount"] = 5
        self.assertIsNone(self.first_usage(self.fixture.report())["total"])
        self.fixture.responses["metrics:eastus2"]["value"][0] = copy.deepcopy(original)
        self.fixture.metric()["id"] = original["id"].replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)
        self.assertIsNone(self.first_usage(self.fixture.report())["total"])

    def test_metric_response_window_is_exact_not_stale_or_coarsened(self):
        self.assertEqual(self.first_usage(self.fixture.report())["total"], 0)
        raw = self.fixture.responses["metrics:eastus2"]
        original = copy.deepcopy(raw)
        for change in ({"interval": "PT6H"}, {"timespan": "2026-09-01T00:00:00Z/2026-09-02T00:00:00Z"}, {"resourceregion": "westus"}):
            self.fixture.responses["metrics:eastus2"] = {**original, **change}
            report = self.fixture.report()
            self.assertIsNone(self.first_usage(report)["total"])
            self.assertIn("metric_window_or_scope_mismatch", self.source(report, "metrics:eastus2")["codes"])

    def test_optional_metric_envelope_fields_do_not_replace_required_resource_identity(self):
        raw = self.fixture.responses["metrics:eastus2"]
        raw["namespace"] = None
        raw["resourceregion"] = None
        report = self.fixture.report()
        self.assertEqual(self.first_usage(report)["total"], 0)
        self.fixture.metric()["id"] = None
        report = self.fixture.report()
        self.assertIsNone(self.first_usage(report)["total"])
        self.assertIn("metric_identity_or_unit_mismatch", self.first_usage(report)["codes"])

    def test_bad_or_stale_pool_evidence_does_not_poison_healthy_observations(self):
        original = self.fixture.evidence()
        self.assertEqual(self.fixture.report(original)["status"], "complete")
        cases = [
            {"subscriptionId": OTHER_SUBSCRIPTION},
            {"observedAt": capacity.utc_text(NOW - timedelta(hours=25))},
            {"observedAt": capacity.utc_text(NOW + timedelta(seconds=1))},
            {"observedAt": "2026-09-09T12:00:00"},
            {"reference": f"https://evil.invalid/{SECRET}"},
            {"schemaVersion": True},
            {"reservePercent": 20},
        ]
        for change in cases:
            with self.subTest(change=change):
                report = self.fixture.report({**original, **change})
                self.assertEqual(report["measurementCoverage"]["status"], "complete")
                self.assertEqual(report["poolCoverage"]["status"], "unknown")
                self.assertEqual(report["pools"], [])
                self.assertEqual(self.first_usage(report)["total"], 0)
                self.assertNotIn(SECRET, json.dumps(report))
                self.assertNotIn(OTHER_SUBSCRIPTION, json.dumps(report))

    def test_incomplete_counter_unit_version_or_membership_never_implies_regional_scope(self):
        good = self.fixture.evidence()
        self.assertEqual(self.fixture.report(good)["status"], "complete")
        for key, value in (("counter", "AIServices.GlobalStandard.unknown"), ("unit", "Bytes")):
            bad = copy.deepcopy(good)
            bad["pools"][0][key] = value
            report = self.fixture.report(bad)
            self.assertEqual(report["pools"][0]["scope"], "global")
            self.assertIsNone(report["pools"][0]["headroom"])
        bad = copy.deepcopy(good)
        bad["pools"][0]["regions"] = ["eastus2"]
        self.assertEqual(self.fixture.report(bad)["poolEvidence"]["error"], "incomplete_pool_membership")
        bad = copy.deepcopy(good)
        bad["pools"][0]["model"]["versions"] = ["2"]
        self.assertEqual(self.fixture.report(bad)["poolEvidence"]["error"], "pool_version_membership_mismatch")
        bad = copy.deepcopy(good)
        bad["pools"][0]["capacityUnitsPerCounterUnit"] = 1000
        self.assertEqual(self.fixture.report(bad)["poolEvidence"]["error"], "unsupported_unit_conversion")

    def test_contradictory_counters_and_platform_evidence_preserve_unknown_headroom(self):
        self.assertEqual(self.fixture.report(self.fixture.evidence())["pools"][0]["headroom"], 160)
        self.fixture.responses["quota:swedencentral"]["value"][0]["currentValue"] = 141
        report = self.fixture.report(self.fixture.evidence())
        self.assertIn("contradictory_pool_counters", report["pools"][0]["codes"])
        self.assertIsNone(report["pools"][0]["headroom"])
        self.fixture.responses["quota:swedencentral"]["value"][0]["currentValue"] = 140
        key = "availability:OpenAI:model-a:1"
        self.fixture.responses[key]["value"][0]["properties"]["availableCapacity"] = 161
        report = self.fixture.report(self.fixture.evidence())
        self.assertIn("contradictory_pool_availability", report["pools"][0]["codes"])
        self.assertIsNone(report["pools"][0]["headroom"])
        self.fixture.responses[key]["value"][0]["properties"]["availableCapacity"] = 150
        self.fixture.responses[key]["value"][0]["properties"]["model"]["version"] = "old"
        report = self.fixture.report(self.fixture.evidence())
        self.assertIn("availability_identity_mismatch", self.source(report, key)["codes"])
        self.assertIsNone(report["pools"][0]["headroom"])

    def test_raw_allocations_never_default_missing_or_bool_to_zero(self):
        self.assertEqual(self.fixture.report()["deployments"][0]["live"]["capacity"], 50)
        row = self.fixture.responses["deployments:eastus2"]["value"][0]
        for value in (None, False, -1, "50"):
            row["sku"]["capacity"] = value
            report = self.fixture.report()
            self.assertIsNone(report["deployments"][0]["live"])
            self.assertIn("invalid_numeric_value", self.source(report, "deployments:eastus2")["codes"])
        row["sku"]["capacity"] = 0
        report = self.fixture.report()
        self.assertEqual(report["deployments"][0]["live"]["capacity"], 0)
        self.assertEqual(report["deployments"][0]["inventoryStatus"], "matched")

    def test_counter_smaller_than_app_allocation_or_over_limit_is_not_headroom(self):
        self.assertEqual(self.fixture.report(self.fixture.evidence())["status"], "complete")
        for current, limit in ((99, 300), (301, 300)):
            with self.subTest(current=current):
                for region in self.fixture.catalog.regions:
                    self.fixture.responses[f"quota:{region}"]["value"][0].update(currentValue=current, limit=limit)
                report = self.fixture.report(self.fixture.evidence())
                pool = report["pools"][0]
                self.assertIsNone(pool["headroom"])
                self.assertIsNone(pool["outsideCatalogOrUnattributedAllocation"])
                self.assertIn("contradictory_pool_allocation", pool["codes"])

    def test_quota_duplicates_missing_fields_and_zero_are_not_normalized(self):
        row = self.fixture.responses["quota:eastus2"]["value"][0]
        row.update(currentValue=0, limit=0)
        self.assertEqual(self.fixture.report()["quotaCounters"][0]["observations"][0]["currentValue"], 0)
        for value in (None, False, "0", -1):
            row["currentValue"] = value
            report = self.fixture.report()
            self.assertEqual(self.source(report, "quota:eastus2")["status"], "unavailable")
            self.assertEqual(len(report["quotaCounters"][0]["observations"]), 1)
        row["currentValue"] = 0
        self.fixture.responses["quota:eastus2"]["value"].append(copy.deepcopy(row))
        report = self.fixture.report()
        self.assertIn("duplicate_quota_counter", self.source(report, "quota:eastus2")["codes"])

    def test_account_discovery_is_exact_ownership_not_prefix_guessing(self):
        self.assertEqual(self.fixture.report()["measurementCoverage"]["status"], "complete")
        first = self.fixture.responses["accounts"]["value"][0]
        original = copy.deepcopy(first)
        for change in (
            {"id": original["id"].replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)},
            {"kind": "OpenAI"}, {"env": "different"}, {"azdEnv": "different"},
            {"location": "swedencentral"}, {"managedBy": "someone-else"},
        ):
            with self.subTest(change=change):
                self.fixture.responses["accounts"]["value"][0] = {**original, **change}
                self.fixture.calls.clear()
                report = self.fixture.report()
                self.assertIsNone(report["deployments"][0]["live"])
                self.assertFalse(any("/deployments?" in c[c.index("--url") + 1] for c in self.fixture.calls))
        self.fixture.responses["accounts"]["value"][0] = copy.deepcopy(original)
        second = {**original, "name": original["name"][:-1] + "n", "id": original["id"][:-1] + "n"}
        self.fixture.responses["accounts"]["value"].append(second)
        self.assertIn("ambiguous_account_inventory", self.source(self.fixture.report(), "accounts")["codes"])

    def test_unknown_accounts_and_prefix_lookalikes_never_gain_ownership(self):
        original = copy.deepcopy(self.fixture.responses["accounts"]["value"][0])
        self.assertEqual(self.fixture.report()["measurementCoverage"]["status"], "complete")
        lookalike = {**original, "name": original["name"] + "-extra", "id": original["id"] + "-extra"}
        self.fixture.responses["accounts"]["value"][0] = lookalike
        report = self.fixture.report()
        self.assertIsNone(report["deployments"][0]["account"])
        self.assertIsNone(report["deployments"][0]["live"])
        self.assertFalse(self.source(report, "deployments:eastus2")["attempted"])

    def test_paged_accounts_cannot_hide_second_owned_account(self):
        self.assertEqual(self.fixture.report()["measurementCoverage"]["status"], "complete")
        self.fixture.responses["accounts"]["nextLink"] = f"https://evil.invalid/{SECRET}"
        self.fixture.calls.clear()
        report = self.fixture.report()
        self.assertEqual(self.source(report, "accounts")["status"], "partial")
        self.assertIsNone(report["deployments"][0]["live"])
        self.assertFalse(any("/deployments?" in c[c.index("--url") + 1] for c in self.fixture.calls))
        self.assertNotIn(SECRET, json.dumps(report))

    def test_private_extra_metadata_and_error_details_are_never_rendered(self):
        self.fixture.responses["group"]["owner"] = SECRET
        self.fixture.responses["accounts"]["value"][0]["properties"] = {"key": SECRET, "endpoint": SECRET}
        self.fixture.responses["deployments:eastus2"]["value"][0]["properties"]["prompt"] = SECRET
        self.fixture.metric()["displayDescription"] = SECRET
        self.fixture.metric()["timeseries"][0]["data"][0]["trace"] = SECRET
        report = self.fixture.report(self.fixture.evidence())
        self.assertEqual(report["status"], "complete")
        self.assertNotIn(SECRET, capacity.render(report, "json"))
        self.assertNotIn(SECRET, capacity.render(report, "text"))

    def test_wrong_group_stops_all_subsequent_reads(self):
        self.fixture.report()
        self.assertGreater(len(self.fixture.calls), 1)
        self.fixture.calls.clear()
        self.fixture.responses["group"]["id"] = self.fixture.scope.group_path.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)
        report = self.fixture.report()
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertIn("resource_group_ownership_mismatch", self.source(report, "group")["codes"])
        self.assertEqual(report["platformAvailability"], [])
        self.assertEqual(report["quotaCounters"], [])
        self.assertEqual(report["recommendations"], [])


class BoundsAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fixture = Fixture(self.directory)

    def test_transport_response_stderr_and_timeout_have_real_controls(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.write('{}')"]
        self.assertEqual(capacity.run_bounded(command, 5, 2).body, b"{}")
        with self.assertRaisesRegex(capacity.EvidenceError, "response_too_large"):
            capacity.run_bounded(command, 5, 1)
        warning = capacity.run_bounded(
            [sys.executable, "-c", f"import sys; print('{{}}'); sys.stderr.write('{SECRET}')"], 5, 64,
        )
        self.assertTrue(warning.warning)
        self.assertNotIn(SECRET.encode(), warning.body)
        for script, code in (
            (f"import sys; sys.stderr.write('{SECRET}'); sys.exit(1)", "azure_read_failed"),
            ("import sys; sys.stderr.write('x' * 65537)", "diagnostics_too_large"),
            ("import time; print('{}', flush=True); time.sleep(10)", "source_deadline_exceeded"),
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(capacity.EvidenceError, code) as caught:
                capacity.run_bounded([sys.executable, "-c", script], 0.5 if "sleep" in script else 5, 64)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertLess(time.monotonic() - started, 5)

    def test_cli_read_primitive_denies_new_endpoints_models_regions_and_writes(self):
        reader = capacity.AzureReader(self.fixture.scope, runner=self.fixture.runner)
        with patch.object(capacity, "az_command", return_value=["mock-az"]):
            reader.read("quota", region="eastus2")
            self.assertTrue(self.fixture.calls)
            self.fixture.calls.clear()
            for operation, args in (
                ("delete", {}), ("set", {}), ("listKeys", {}),
                ("quota", {"region": "westus"}),
                ("availability", {"model": capacity.Model("OpenAI", "not-catalogued", "1", "Standard")}),
                ("deployments", {"account": "arbitrary-account", "region": "eastus2"}),
                ("metrics", {"account": self.fixture.accounts["eastus2"], "region": "eastus2",
                             "window": self.fixture.window, "metrics": ("AzureOpenAIRequests",)}),
            ):
                with self.subTest(operation=operation), self.assertRaises(capacity.EvidenceError):
                    reader.read(operation, **args)
            self.assertEqual(self.fixture.calls, [])

    def test_read_call_wallclock_and_total_byte_budgets_prevent_next_egress(self):
        clock = [0.0]
        reader = capacity.AzureReader(self.fixture.scope, runner=self.fixture.runner, clock=lambda: clock[0])
        with patch.object(capacity, "az_command", return_value=["mock-az"]):
            reader.read("quota", region="eastus2")
            self.assertEqual(len(self.fixture.calls), 1)
            clock[0] = capacity.COLLECTION_SECONDS
            with self.assertRaisesRegex(capacity.EvidenceError, "collection_deadline_exceeded"):
                reader.read("quota", region="eastus2")
            self.assertEqual(len(self.fixture.calls), 1)
            clock[0] = 0
            reader.calls = capacity.MAX_CALLS
            with self.assertRaisesRegex(capacity.EvidenceError, "read_limit_exceeded"):
                reader.read("quota", region="eastus2")
            reader.calls = 1
            reader.bytes = capacity.MAX_TOTAL_RESPONSE_BYTES
            with self.assertRaisesRegex(capacity.EvidenceError, "response_budget_exceeded"):
                reader.read("quota", region="eastus2")
            self.assertEqual(len(self.fixture.calls), 1)

    def test_failed_real_processes_consume_response_budget(self):
        allowances = []
        exit_code = [0]

        def failed_process(_command, timeout, limit):
            allowances.append(limit)
            return capacity.run_bounded(
                [sys.executable, "-c", f"import sys; sys.stdout.write('{{\"value\":[]}}'); sys.exit({exit_code[0]})"],
                timeout, limit,
            )

        reader = capacity.AzureReader(self.fixture.scope, runner=failed_process)
        with (
            patch.object(capacity, "MAX_RESPONSE_BYTES", 24),
            patch.object(capacity, "MAX_TOTAL_RESPONSE_BYTES", 32),
            patch.object(capacity, "az_command", return_value=["mock-az"]),
        ):
            self.assertEqual(reader.read("quota", region="eastus2").document, {"value": []})
            self.assertEqual(reader.bytes, 12)
            reader.bytes = 0
            allowances.clear()
            exit_code[0] = 1
            for expected_code, consumed in (("azure_read_failed", 12), ("azure_read_failed", 24), ("response_too_large", 32)):
                with self.assertRaisesRegex(capacity.EvidenceError, expected_code):
                    reader.read("quota", region="eastus2")
                self.assertEqual(reader.bytes, consumed)
            with self.assertRaisesRegex(capacity.EvidenceError, "response_budget_exceeded"):
                reader.read("quota", region="eastus2")
            self.assertEqual(allowances, [24, 20, 8])

    def test_timeout_and_unknown_error_accounting_cannot_refresh_byte_allowance(self):
        with self.assertRaises(capacity.EvidenceError) as caught:
            capacity.run_bounded(
                [sys.executable, "-c", "import sys,time; sys.stdout.write('x' * 12); sys.stdout.flush(); time.sleep(10)"],
                0.5, 24,
            )
        self.assertEqual(caught.exception.response_bytes, 12)

        def unknown_failure(*_args):
            raise capacity.EvidenceError("azure_read_failed")

        reader = capacity.AzureReader(self.fixture.scope, runner=unknown_failure)
        with (
            patch.object(capacity, "MAX_RESPONSE_BYTES", 24),
            patch.object(capacity, "MAX_TOTAL_RESPONSE_BYTES", 24),
            patch.object(capacity, "az_command", return_value=["mock-az"]),
        ):
            with self.assertRaisesRegex(capacity.EvidenceError, "azure_read_failed"):
                reader.read("quota", region="eastus2")
            self.assertEqual(reader.bytes, 24)
            with self.assertRaisesRegex(capacity.EvidenceError, "response_budget_exceeded"):
                reader.read("quota", region="eastus2")

    def test_deadline_failures_still_produce_dated_partial_report(self):
        reader = capacity.AzureReader(self.fixture.scope, runner=self.fixture.runner, clock=lambda: 0)
        reader.deadline = -1
        report = self.fixture.report(reader=reader)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["sources"][0]["codes"], ["collection_deadline_exceeded"])
        self.assertEqual(report["finishedAt"], capacity.utc_text(NOW))
        self.assertEqual(report["window"], self.fixture.window.public())
        self.assertEqual(self.fixture.calls, [])

    def test_strict_json_and_local_bytes_fail_closed(self):
        self.assertEqual(capacity.strict_json(b'{"x":0}'), {"x": 0})
        for body in (b'{"x":0,"x":1}', b'{"x":NaN}', b'{"x":Infinity}', b"", b"\xff"):
            with self.subTest(body=body), self.assertRaises(capacity.EvidenceError):
                capacity.strict_json(body)
        path = self.directory / "bounded.json"
        path.write_bytes(b"{}")
        self.assertEqual(capacity.read_json(path, 2)[0], {})
        with self.assertRaisesRegex(capacity.EvidenceError, "local_evidence_too_large"):
            capacity.read_json(path, 1)

    def test_window_days_utc_and_bounds(self):
        self.assertEqual(capacity.Window.create(1, None, NOW).hours, 24)
        self.assertEqual(capacity.Window.create(7, None, NOW).hours, 168)
        for days in (0, 8, True, 1.0):
            with self.assertRaises(capacity.EvidenceError):
                capacity.Window.create(days, None, NOW)
        for end in (
            "2026-09-09T11:30:00Z", "2026-09-09T12:00:00Z",
            "2026-09-07T11:00:00Z", "2026-09-09T11:00:00", "2026-09-09T11:00:00-05:00",
        ):
            with self.subTest(end=end), self.assertRaises(capacity.EvidenceError):
                capacity.Window.create(1, end, NOW)

    def test_point_bounds_have_valid_full_week_control(self):
        fixture = Fixture(self.directory, days=7)
        report = fixture.report(fixture.evidence())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["deployments"][0]["usage"]["ModelRequests"]["samples"], 168)
        series = fixture.metric()["timeseries"][0]
        series["data"].append({"timeStamp": capacity.utc_text(fixture.window.end), "total": 0})
        report = fixture.report()
        self.assertIsNone(report["deployments"][0]["usage"]["ModelRequests"]["total"])
        self.assertIn("row_limit_exceeded", report["deployments"][0]["usage"]["ModelRequests"]["codes"])
        budget = capacity.PointBudget(168)
        budget.consume(168)
        with self.assertRaisesRegex(capacity.EvidenceError, "point_budget_exceeded"):
            budget.consume(1)

    def test_collection_point_budget_is_shared_across_accounts(self):
        expected = 2 * len(capacity.METRICS) * 24
        with patch.object(capacity, "MAX_TOTAL_POINTS", expected):
            report = self.fixture.report(self.fixture.evidence())
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["consumed"]["points"], expected)
        with patch.object(capacity, "MAX_TOTAL_POINTS", expected - 1):
            report = self.fixture.report(self.fixture.evidence())
            source = next(s for s in report["sources"] if s["id"] == "metrics:swedencentral")
            self.assertIn("point_budget_exceeded", source["codes"])
            self.assertIsNone(report["deployments"][1]["usage"]["ModelRequests"]["total"])
            self.assertEqual(report["deployments"][0]["usage"]["ModelRequests"]["total"], 0)

    def test_account_and_total_deployment_limits_have_matching_controls(self):
        with patch.object(capacity, "MAX_ACCOUNTS", 2):
            self.assertEqual(self.fixture.report()["measurementCoverage"]["status"], "complete")
            extra = copy.deepcopy(self.fixture.responses["accounts"]["value"][0])
            extra["name"] = "not-our-account"
            extra["id"] = self.fixture.scope.account_path(extra["name"])
            self.fixture.responses["accounts"]["value"].append(extra)
            report = self.fixture.report()
            source = next(s for s in report["sources"] if s["id"] == "accounts")
            self.assertIn("row_limit_exceeded", source["codes"])
        self.fixture.responses["accounts"]["value"].pop()
        with patch.object(capacity, "MAX_DEPLOYMENTS", 2):
            self.assertEqual(self.fixture.report()["measurementCoverage"]["status"], "complete")
            extra = copy.deepcopy(self.fixture.responses["deployments:eastus2"]["value"][0])
            extra["name"] = "not-our-deployment"
            extra["id"] = extra["id"].rsplit("/", 1)[0] + "/" + extra["name"]
            self.fixture.responses["deployments:eastus2"]["value"].append(extra)
            report = self.fixture.report()
            source = next(s for s in report["sources"] if s["id"] == "deployments:swedencentral")
            self.assertIn("deployment_limit_exceeded", source["codes"])
            self.assertIsNone(report["deployments"][1]["live"])

    def test_metadata_and_catalog_count_limits_are_not_silent_truncation(self):
        row = self.fixture.responses["quota:eastus2"]["value"][0]
        document = {"value": [
            {**row, "name": {"value": f"OpenAI.Standard.model-{index}"}} for index in range(capacity.MAX_ROWS)
        ]}
        self.assertEqual(len(capacity.parse_quota(document)), capacity.MAX_ROWS)
        document["value"].append({**row, "name": {"value": "OpenAI.Standard.extra"}})
        with self.assertRaisesRegex(capacity.EvidenceError, "row_limit_exceeded"):
            capacity.parse_quota(document)
        raw = catalog_document()
        with patch.object(capacity, "MAX_REGIONS", 2):
            write_json(self.fixture.catalog_path, raw)
            self.assertEqual(len(capacity.load_catalog(self.fixture.catalog_path).regions), 2)
            raw["regions"]["westus"] = {"dataZone": "US"}
            write_json(self.fixture.catalog_path, raw)
            with self.assertRaisesRegex(capacity.EvidenceError, "region_limit_exceeded"):
                capacity.load_catalog(self.fixture.catalog_path)

    def test_report_bytes_include_escaped_serialization(self):
        report = self.fixture.report()
        text = capacity.render(report, "json")
        with patch.object(capacity, "MAX_REPORT_BYTES", len(text.encode())):
            self.assertEqual(capacity.render(report, "json"), text)
        with (
            patch.object(capacity, "MAX_REPORT_BYTES", len(text.encode()) - 1),
            self.assertRaisesRegex(capacity.EvidenceError, "report_too_large"),
        ):
            capacity.render(report, "json")

    def test_text_artifacts_retain_the_subscription_binding_without_identity(self):
        report = self.fixture.report()
        text = capacity.render(report, "text")
        self.assertIn(report["scope"]["subscriptionFingerprint"], text)
        self.assertNotIn(SUBSCRIPTION, text)
        modified = copy.deepcopy(report)
        modified["scope"]["subscriptionFingerprint"] = "a" * 64
        self.assertNotEqual(capacity.render(modified, "text"), text)

    def test_cli_new_artifact_is_exclusive_and_invalid_input_never_reads_azure(self):
        report = self.fixture.report(self.fixture.evidence())
        path = self.directory / "new-report.json"
        args = [
            "--subscription", SUBSCRIPTION, "--resource-group", self.fixture.scope.resource_group,
            "--environment-name", "example", "--format", "json", "--output", str(path),
        ]
        with (
            patch.object(cli, "load_catalog", return_value=self.fixture.catalog),
            patch.object(cli, "collect", return_value=report) as collect_mock,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(json.loads(output.getvalue()), report)
            before = path.read_bytes()
            self.assertEqual(json.loads(before), report)
            collect_mock.reset_mock()
            output.seek(0)
            output.truncate()
            self.assertEqual(cli.main(args), 2)
            collect_mock.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn(str(path), output.getvalue())
        with patch.object(cli, "collect") as collect_mock, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main([
                "--subscription", SECRET, "--resource-group", "rg", "--environment-name", "example", "--format", "json",
            ]), 2)
            collect_mock.assert_not_called()
            self.assertNotIn(SECRET, output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as caught:
            cli.main(["--apply", SECRET])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn(SECRET, errors.getvalue())

    def test_exclusive_open_also_protects_file_created_during_collection(self):
        report = self.fixture.report()
        path = self.directory / "raced-report.json"

        def concurrent_file(*_args):
            path.write_bytes(b"concurrent-owner")
            return report

        with (
            patch.object(cli, "load_catalog", return_value=self.fixture.catalog),
            patch.object(cli, "collect", side_effect=concurrent_file),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main([
                "--subscription", SUBSCRIPTION, "--resource-group", self.fixture.scope.resource_group,
                "--environment-name", "example", "--output", str(path), "--format", "json",
            ]), 2)
            self.assertEqual(path.read_bytes(), b"concurrent-owner")
            self.assertEqual(json.loads(output.getvalue())["error"], "output_already_exists")

    def test_provider_formats_with_spaces_remain_exact_and_url_encoded(self):
        document = catalog_document()
        document["catalog"][0]["format"] = "Black Forest Labs"
        fixture = Fixture(self.directory, document)
        report = fixture.report(fixture.evidence())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["deployments"][0]["live"]["model"]["format"], "Black Forest Labs")
        self.assertTrue(any("modelFormat=Black+Forest+Labs" in c[c.index("--url") + 1] for c in fixture.calls))

    def test_real_catalog_stays_read_only_and_uses_existing_naming(self):
        paths = [ROOT / "infra" / "models.json", ROOT / "scripts" / "sync-model-capacity.py"]
        before = {path: path.read_bytes() for path in paths}
        catalog = capacity.load_catalog(paths[0])
        naming = load_script("capacity_original_naming_test", paths[1])
        raw = json.loads(before[paths[0]])
        expected = {
            (d["region"], naming.deployment_name(raw, m["name"], d))
            for m in raw["catalog"] for d in m["deployments"]
        }
        self.assertEqual({(d.region, d.name) for d in catalog.deployments}, expected)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_cli_help_has_no_live_side_effects(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "report-model-capacity.py"), "--help"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--pool-evidence", completed.stdout)
        self.assertNotIn("--apply", completed.stdout)


if __name__ == "__main__":
    unittest.main()
