"""Offline controls for dated admission, evidence provenance and read-only reporting."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.tests._loader import load_script
from scripts.tests._platform import find_bash

ROOT = Path(__file__).resolve().parents[2]
AVAILABILITY = load_script("retirement_preflight", ROOT / "scripts" / "check-model-availability.py")
RETIREMENT = AVAILABILITY.retirement
NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
WORKFLOW = ROOT / ".github" / "workflows" / "model-retirements.yml"
CATALOG = {
    "naming": {
        "foundryToken": "example",
        "subscriptionToken": "test",
        "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
        "skuShort": {"GlobalStandard": "glbl", "Standard": "std"},
    },
    "regions": {"eastus2": {"primary": True}},
    "catalog": [{
        "name": "example-model", "format": "OpenAI",
        "deployments": [{"region": "eastus2", "sku": "GlobalStandard", "version": "1", "capacity": 50}],
    }],
}
DESIRED = AVAILABILITY.catalog_requirements(CATALOG)["eastus2"][0]
ACCOUNT = "mf-example-prod-eastus2-privateaccount"
READ_ENV = {
    "AZURE_SUBSCRIPTION_ID": "private-subscription-id",
    "AZURE_RESOURCE_GROUP": "private-resource-group",
    "AZURE_ENV_NAME": "prod",
    "AI4IA_MODEL_CAPACITY_PROFILE": "baseline",
    "AI4IA_CLAUDE_ENABLED": "false",
}
PUBLIC_URL = "https://learn.microsoft.com/azure/foundry/openai/concepts/model-retirement-schedule"


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def offered(date=None, *, version="1", lifecycle="GenerallyAvailable", **model_fields):
    model = {
        "name": DESIRED["name"], "format": DESIRED["format"], "version": version,
        "lifecycleStatus": lifecycle,
        "skus": [{"name": DESIRED["sku"], "deprecationDate": date}],
        **model_fields,
    }
    return {"model": model}


def deployed(**changes):
    return {
        "accountName": ACCOUNT,
        "deploymentName": DESIRED["deploymentName"],
        "region": DESIRED["region"],
        "modelName": DESIRED["name"],
        "format": DESIRED["format"],
        "version": DESIRED["version"],
        "sku": DESIRED["sku"],
        "capacity": DESIRED["capacity"],
        "versionUpgradeOption": "NoAutoUpgrade",
        "provisioningState": "Succeeded",
        **changes,
    }


def inventory(record):
    return {} if record is None else {
        (record["region"], record["deploymentName"].casefold()): record
    }


def observation(rows, *, actual=None, target=None, public=(), inventory_state="observed"):
    target = dict(DESIRED) if target is None else target
    return AVAILABILITY.retirement_observations(
        [target], rows, inventory(actual), region=target["region"], now=NOW,
        inventory_state=inventory_state, inventory_observed_at=NOW, public=public,
    )[0]


def instant(days=0, microseconds=0):
    return RETIREMENT.timestamp(NOW + timedelta(days=days, microseconds=microseconds))


def public_record(**changes):
    return {
        "name": DESIRED["name"], "format": "OpenAI", "version": "1",
        "region": "eastus2", "sku": "GlobalStandard", "source_url": PUBLIC_URL,
        "observed_at": instant(-1), "date": instant(1), "qualifier": "exact",
        **changes,
    }


def public_observations(*records):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "public.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        return RETIREMENT.load_public_observations(path, NOW)


def report(observations, sources=None, public_count=0):
    return RETIREMENT.build_report(
        observations,
        sources if sources is not None else [
            RETIREMENT.SourceRead("fixture", "eastus2", "observed", instant())
        ],
        now=NOW, catalog_bytes=json.dumps(CATALOG).encode(), capacity_profile="baseline",
        include_anthropic=False, public_count=public_count,
    )


class DateBoundaryTests(unittest.TestCase):
    def test_exact_windows_and_each_adjacent_instant(self):
        cases = [
            (None, "unknown"), (timedelta(microseconds=-1), "expired"),
            (timedelta(0), "expired"), (timedelta(microseconds=1), "7-day"),
            (timedelta(days=7), "7-day"),
            (timedelta(days=7, microseconds=1), "30-day"),
            (timedelta(days=30), "30-day"),
            (timedelta(days=30, microseconds=1), "90-day"),
            (timedelta(days=90), "90-day"),
            (timedelta(days=90, microseconds=1), "beyond-90-days"),
        ]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                date = None if delta is None else NOW + delta
                self.assertEqual(RETIREMENT.warning_window(date, NOW), expected)

    def test_offset_equivalence_and_utc_day_boundary(self):
        for raw in (
            "2026-09-15T12:00:00Z",
            "2026-09-15T17:30:00+05:30",
            "2026-09-15T05:00:00-07:00",
            "2026-09-15t12:00:00.0000000z",
        ):
            with self.subTest(raw=raw):
                date, status, precision = RETIREMENT.parse_date(raw)
                self.assertEqual((status, precision), ("known", "instant"))
                self.assertEqual(RETIREMENT.warning_window(date, NOW), "7-day")
                self.assertEqual(
                    RETIREMENT.warning_window(date, NOW.astimezone(timezone(timedelta(hours=-7)))),
                    "7-day",
                )
        midnight, status, precision = RETIREMENT.parse_date("2026-09-09")
        self.assertEqual((status, precision), ("known", "day"))
        self.assertEqual(midnight, datetime(2026, 9, 9, tzinfo=UTC))
        self.assertEqual(RETIREMENT.warning_window(midnight, midnight), "expired")
        self.assertEqual(
            RETIREMENT.warning_window(midnight, midnight - timedelta(microseconds=1)),
            "7-day",
        )

    def test_missing_malformed_and_naive_values_never_become_dates(self):
        for raw in (None, ""):
            self.assertEqual(RETIREMENT.parse_date(raw), (None, "missing", None))
        for raw in (
            "2026-02-29", "2026-13-01", "09/09/2026", "no earlier than 2026-09-09",
            "2026-09-09T00:00:00", "2026-09-09T00:00:00-00:00",
            "2026-09-09T00:00:00+25:00", "2026-09-09T00:00:60Z",
            "2026-09-09T00:00:00+00:60", "2026-09-09T00:00:00+00:99",
            "2026-09-09T00:00:00-00:60", "2026-09-09T00:00:00-01:99",
            " 2026-09-09", 0, True, [], {"date": "2026-09-09"},
        ):
            with self.subTest(raw=raw):
                self.assertEqual(RETIREMENT.parse_date(raw), (None, "malformed", None))
        self.assertEqual(RETIREMENT.parse_date("2028-02-29")[1], "known")
        self.assertEqual(RETIREMENT.parse_date("2026-09-09T00:00:00+00:59")[1], "known")
        with self.assertRaises(ValueError):
            RETIREMENT.warning_window(NOW, NOW.replace(tzinfo=None))

    def test_nonzero_submicrosecond_precision_is_unknown_not_silently_rounded(self):
        for date, expected in (
            ("2026-09-15T12:00:00.0000000Z", "known"),
            ("2026-09-15T12:00:00.0000001Z", "unsupported"),
            ("2026-09-15T12:00:00.000000001Z", "unsupported"),
            ("2026-09-15T12:00:00.0000010Z", "known"),
        ):
            with self.subTest(date=date):
                self.assertEqual(RETIREMENT.parse_date(date)[1], expected)
                observed = observation([offered(date)])
                if expected == "unsupported":
                    self.assertTrue(observed.incomplete)
                    self.assertEqual(observed.decision, "no-authoritative-block")


class AdmissionPolicyTests(unittest.TestCase):
    def test_every_catalog_identity_survives_the_report_projection(self):
        catalog = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        required = AVAILABILITY.catalog_requirements(catalog)
        for records in required.values():
            for record in records:
                with self.subTest(name=record["name"], format=record["format"]):
                    state = RETIREMENT.ModelState.from_record(record, deployed=False)
                    for field in ("name", "format", "version", "sku", "capacity"):
                        self.assertEqual(getattr(state, field), record[field])

    def test_authoritative_horizon_has_same_fixture_positive_controls(self):
        for days, extra, expected in (
            (-1, 0, "block-addition-or-change"),
            (0, 0, "block-addition-or-change"),
            (7, 0, "block-addition-or-change"),
            (7, 1, "no-authoritative-block"),
            (30, 0, "no-authoritative-block"),
            (90, 0, "no-authoritative-block"),
        ):
            with self.subTest(days=days, extra=extra):
                observed = observation([offered(instant(days, extra))])
                self.assertEqual(observed.decision, expected)
                self.assertTrue(observed.attention)  # The absent deployment is still a drift finding.

    def test_exact_succeeded_expired_deployment_warns_instead_of_disappearing(self):
        for lifecycle in ("GenerallyAvailable", "Deprecating", "Deprecated"):
            rows = [offered(instant(-1), lifecycle=lifecycle)]
            exact = observation(rows, actual=deployed())
            added = observation(rows)
            self.assertEqual(exact.decision, "reconcile-warning")
            self.assertEqual(added.decision, "block-addition-or-change")
            self.assertFalse(exact.drift)
            summary = RETIREMENT.observation_summary(exact)
            for text in ("expired", "NoAutoUpgrade", "example-model@1", "deployed=", "observed="):
                self.assertIn(text, summary)

    def test_every_unsafe_change_loses_the_exact_reconcile_exception(self):
        rows = [offered(instant(7))]
        self.assertEqual(observation(rows, actual=deployed()).decision, "reconcile-warning")
        for change in (
            {"modelName": "another-model"}, {"format": "Anthropic"}, {"version": "2"},
            {"sku": "Standard"}, {"capacity": 51},
            {"versionUpgradeOption": "OnceCurrentVersionExpired"},
            {"provisioningState": "Failed"},
        ):
            with self.subTest(change=change):
                changed = observation(rows, actual=deployed(**change))
                self.assertEqual(changed.decision, "block-addition-or-change")
                self.assertTrue(changed.drift)
                safe = observation([offered(instant(91))], actual=deployed(**change))
                self.assertEqual(safe.decision, "no-authoritative-block")

    def test_malformed_capacity_cannot_gain_an_exact_reconcile_exception(self):
        rows = [offered(instant(7))]
        self.assertEqual(observation(rows, actual=deployed()).decision, "reconcile-warning")
        for capacity in (None, "50", 50.1, True):
            with self.subTest(capacity=capacity):
                observed = observation(rows, actual=deployed(capacity=capacity))
                self.assertEqual(observed.decision, "block-addition-or-change")
                self.assertTrue(observed.incomplete)

    def test_safe_target_upgrade_keeps_old_retired_evidence_without_blocking_it(self):
        target = {**DESIRED, "version": "2"}
        rows = [
            offered(instant(-1), lifecycle="Deprecated"),
            offered(instant(91), version="2"),
        ]
        safe = observation(rows, target=target, actual=deployed())
        self.assertEqual(safe.decision, "no-authoritative-block")
        self.assertTrue(any(e.unsafe for e in safe.deployed_evidence))
        self.assertFalse(any(e.unsafe for e in safe.catalog_evidence))
        self.assertEqual(safe.drift, ("version",))
        rows[1] = offered(instant(7), version="2")
        self.assertEqual(
            observation(rows, target=target, actual=deployed()).decision,
            "block-addition-or-change",
        )

    def test_lifecycle_is_authoritative_only_for_known_api_states(self):
        for lifecycle, expected in (
            ("Deprecating", "block-addition-or-change"),
            ("Deprecated", "block-addition-or-change"),
            ("DEPRECATING", "block-addition-or-change"),
            ("Preview", "no-authoritative-block"),
            ("GenerallyAvailable", "no-authoritative-block"),
            ("Stable", "no-authoritative-block"),
            ("Retired", "no-authoritative-block"),  # Portal name is not an API enum.
            (None, "no-authoritative-block"),
        ):
            with self.subTest(lifecycle=lifecycle):
                observed = observation([offered(instant(91), lifecycle=lifecycle)])
                self.assertEqual(observed.decision, expected)
                self.assertEqual(observed.incomplete, lifecycle in {"Retired", None})

    def test_dates_missing_malformed_or_outside_target_scope_are_not_guessed(self):
        for date in (None, "", "not a date", "2026-09-09T00:00:00"):
            with self.subTest(date=date):
                observed = observation([offered(date)])
                self.assertEqual(observed.decision, "no-authoritative-block")
                self.assertTrue(observed.incomplete)
        for change in (
            {"name": "other-model"}, {"format": "Anthropic"}, {"version": "other"},
            {"skus": [{"name": "Standard", "deprecationDate": instant(-1)}]},
        ):
            with self.subTest(change=change):
                wrong = offered(instant(-1), **change)
                observed = observation([wrong])
                self.assertTrue(observed.incomplete)
                self.assertEqual(observed.decision, "no-authoritative-block")
                control = observation([wrong, offered(instant(-1))])
                self.assertEqual(control.decision, "block-addition-or-change")

    def test_finetune_dates_are_not_inference_dates(self):
        base = offered(instant(91), deprecation={"fineTune": instant(-1)})
        self.assertEqual(observation([base]).decision, "no-authoritative-block")
        base["model"]["deprecation"]["inference"] = instant(-1)
        observed = observation([base])
        self.assertEqual(observed.decision, "block-addition-or-change")
        self.assertTrue(observed.conflicts)
        self.assertEqual(
            {e.value for e in observed.catalog_evidence if e.field != "model.lifecycleStatus"},
            {instant(91), instant(-1)},
        )

    def test_contradictory_subscription_rows_cannot_overwrite_unsafe_evidence(self):
        safe = offered(instant(91))
        unsafe = offered(instant(-1), lifecycle="Deprecating")
        for rows in ([safe, unsafe], [unsafe, safe]):
            observed = observation(rows)
            self.assertEqual(observed.decision, "block-addition-or-change")
            self.assertEqual(len(observed.conflicts), 2)
            self.assertEqual(
                AVAILABILITY.index_lifecycle(rows)["example-model"]["1"], "Deprecating"
            )
        exact_duplicates = observation([safe, safe])
        self.assertFalse(exact_duplicates.conflicts)
        self.assertEqual(len(exact_duplicates.catalog_evidence), 2)

    def test_unlisted_deployment_is_observed_but_not_an_admission_target(self):
        actual = deployed(deploymentName="retained-model")
        observed = AVAILABILITY.retirement_observations(
            [], [offered(instant(-1))], inventory(actual), region="eastus2", now=NOW,
            inventory_state="observed", inventory_observed_at=NOW,
        )[0]
        self.assertIsNone(observed.catalog)
        self.assertEqual(observed.decision, "observe-only")
        self.assertTrue(observed.attention)
        self.assertEqual(observed.drift, ("deployment is outside the catalog",))


class PublicEvidenceTests(unittest.TestCase):
    def test_public_deadline_and_subscription_deadline_remain_independent(self):
        public = public_observations(public_record(date=instant(-1)))
        safe = observation([offered(instant(91))], public=public)
        self.assertEqual(safe.decision, "no-authoritative-block")
        self.assertTrue(safe.conflicts)
        evidence = next(e for e in safe.catalog_evidence if e.source == "public")
        self.assertFalse(evidence.authoritative)
        self.assertFalse(evidence.unsafe)
        self.assertEqual(evidence.observed_at, instant(-1))
        self.assertEqual(evidence.reference, PUBLIC_URL)
        public_later = public_observations(public_record(date=instant(91)))
        unsafe = observation([offered(instant(-1))], public=public_later)
        self.assertEqual(unsafe.decision, "block-addition-or-change")
        self.assertTrue(unsafe.conflicts)

    def test_lower_bound_is_not_relabelled_as_a_confirmed_deadline(self):
        public = public_observations(public_record(date=instant(-1), qualifier="not-before"))
        observed = observation([offered(instant(91))], actual=deployed(), public=public)
        self.assertFalse(observed.conflicts)
        self.assertEqual(observed.decision, "no-authoritative-block")
        self.assertIn("not-before", RETIREMENT.render_report(report([observed], public_count=1)))

    def test_public_scope_is_exact_and_dates_are_not_inferred(self):
        for change in (
            {"name": "other"}, {"format": "other"}, {"version": "2"},
            {"region": "westus"}, {"sku": "Standard"},
        ):
            with self.subTest(change=change):
                public = public_observations(public_record(**change))
                observed = observation([offered(instant(91))], actual=deployed(), public=public)
                self.assertFalse(any(e.source == "public" for e in observed.catalog_evidence))
        for date in (None, "no earlier than September 9", "PRIVATE_RAW_DATE_ERROR"):
            public = public_observations(public_record(date=date))
            observed = observation([offered(instant(91))], actual=deployed(), public=public)
            self.assertTrue(observed.incomplete)
            self.assertEqual(observed.decision, "no-authoritative-block")
            self.assertNotIn("PRIVATE_RAW_DATE_ERROR", RETIREMENT.report_json(report([observed])))

    def test_public_source_contract_rejects_unscoped_or_private_metadata(self):
        public_observations(public_record())  # Identical valid fixture reaches the parser.
        for change in (
            {"source_url": "http://learn.microsoft.com/azure/example"},
            {"source_url": "https://user:password@learn.microsoft.com/azure/example"},
            {"source_url": PUBLIC_URL + "?token=private"},
            {"source_url": "https://private.invalid/azure/example"},
            {"source_url": "https://learn.microsoft.com/answers/a/1"},
            {"source_url": PUBLIC_URL + "\nINJECT"},
            {"observed_at": instant(1)},
            {"observed_at": "2026-09-07"},
            {"observed_at": "2026-09-07T00:00:00"},
            {"qualifier": "estimated"},
            {"region": "*"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                public_observations(public_record(**change))
        missing = public_record()
        del missing["region"]
        with self.assertRaises(ValueError):
            public_observations(missing)
        with self.assertRaises(ValueError):
            public_observations(*[public_record()] * (RETIREMENT.MAX_PUBLIC_RECORDS + 1))


class ReportRenderingTests(unittest.TestCase):
    def test_report_is_complete_clear_only_with_known_clean_evidence(self):
        exact = observation([offered(instant(91))], actual=deployed())
        clean = report([exact])
        self.assertEqual(clean["status"], "clear")
        self.assertEqual(clean["unknown_observations"], 0)
        self.assertEqual(clean["public_comparison"], "not-supplied")
        for rows in ([offered(None)], [offered("private error")], []):
            with self.subTest(rows=rows):
                incomplete = report([observation(rows, actual=deployed())])
                self.assertEqual(incomplete["status"], "incomplete")
                self.assertEqual(incomplete["unknown_observations"], 1)
        attention = report([observation([offered(instant(90))], actual=deployed())])
        self.assertEqual(attention["status"], "attention")

    def test_unavailable_inventory_is_not_an_absent_deployment_or_clean_report(self):
        known = observation([offered(instant(-1))], inventory_state="observed")
        unknown = observation([offered(instant(-1))], inventory_state="unavailable")
        self.assertEqual(known.decision, "block-addition-or-change")
        self.assertEqual(unknown.decision, "unknown")
        self.assertEqual(report([unknown])["status"], "incomplete")
        self.assertEqual(unknown.drift, ("inventory unavailable",))
        self.assertIn("deployed=unknown", RETIREMENT.observation_summary(unknown))
        empty = report([])
        self.assertEqual(empty["status"], "incomplete")
        self.assertIn("not a clean inventory", RETIREMENT.render_report(empty))

    def test_reports_preserve_timestamps_and_do_not_publish_raw_azure_metadata(self):
        actual = deployed(
            id="/subscriptions/private-subscription-id/accounts/private-account",
            endpoint="https://private.endpoint.invalid", tags={"secret": "PRIVATE_TAG"},
        )
        row = offered(
            instant(91), endpoint="https://private.offering.invalid",
            cost={"key": "PRIVATE_OFFERING_KEY"},
        )
        rendered = report([observation([row], actual=actual)])
        serialized = RETIREMENT.report_json(rendered)
        markdown = RETIREMENT.render_report(rendered)
        for text in (serialized, markdown):
            for secret in (
                ACCOUNT, "private-subscription-id", "private-account",
                "private.endpoint", "private.offering", "PRIVATE_TAG", "PRIVATE_OFFERING_KEY",
            ):
                self.assertNotIn(secret, text)
            for value in (instant(), "NoAutoUpgrade", "example-model", "GlobalStandard"):
                self.assertIn(value, text)
        self.assertEqual(len(rendered["catalog_sha256"]), 64)

    def test_omission_and_serialized_byte_limits_fail_visibly_incomplete(self):
        observed = observation([offered(instant(91))], actual=deployed())
        normal = report([observed])
        self.assertEqual(normal["status"], "clear")
        huge = report([observed] * (RETIREMENT.MAX_OBSERVATIONS + 10))
        self.assertEqual(huge["status"], "incomplete")
        self.assertGreaterEqual(huge["omitted_observations"], 10)
        self.assertEqual(
            len(huge["observations"]) + huge["omitted_observations"],
            huge["total_observations"],
        )
        self.assertLessEqual(len(RETIREMENT.report_json(huge).encode()), RETIREMENT.MAX_REPORT_BYTES)
        self.assertLessEqual(len(RETIREMENT.render_report(huge).encode()), RETIREMENT.MAX_MARKDOWN_BYTES)
        with patch.object(RETIREMENT, "MAX_REPORT_BYTES", 1800):
            small_budget = report([observed] * 2)
        self.assertEqual(small_budget["status"], "incomplete")
        self.assertGreater(small_budget["omitted_observations"], 0)
        self.assertLessEqual(len(RETIREMENT.report_json(small_budget).encode()), 1800)

    def test_docs_preview_is_generated_from_exact_report_evidence_only(self):
        document = (ROOT / "docs" / "region-capability-matrix.md").read_text(encoding="utf-8")
        observed = observation([offered(instant(7))], actual=deployed())
        data = report([observed])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.assertEqual(RETIREMENT.write_report(output, data, document), 1)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"model-retirements.json", "model-retirements.md", "region-capability-matrix.md"},
            )
            self.assertEqual(json.loads((output / "model-retirements.json").read_text()), json.loads(RETIREMENT.report_json(data)))
            markdown = (output / "model-retirements.md").read_text(encoding="utf-8")
            preview = (output / "region-capability-matrix.md").read_text(encoding="utf-8")
            self.assertEqual(markdown, RETIREMENT.render_report(data))
            self.assertEqual(
                preview.split(RETIREMENT.DOC_START)[1].split(RETIREMENT.DOC_END)[0],
                "\n" + markdown,
            )
            for marker, part in ((RETIREMENT.DOC_START, 0), (RETIREMENT.DOC_END, 1)):
                self.assertEqual(preview.split(marker)[part], document.split(marker)[part])
        self.assertEqual((ROOT / "docs" / "region-capability-matrix.md").read_text(encoding="utf-8"), document)
        for invalid in ("no markers", RETIREMENT.DOC_END + RETIREMENT.DOC_START, document + RETIREMENT.DOC_START):
            with self.assertRaises(ValueError):
                RETIREMENT.replace_docs_section(invalid, markdown)


class FakeAzure:
    """Only explicit management-plane reads are implemented; anything else fails the test."""

    def __init__(self, rows=None, actual=None, fail=None, malformed=None, allow_quota=False):
        self.rows = [offered(instant(91))] if rows is None else rows
        self.actual = deployed() if actual is None else actual
        self.fail = fail
        self.malformed = malformed
        self.allow_quota = allow_quota
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("account", "show"):
            source = "context"
            payload = {"id": READ_ENV["AZURE_SUBSCRIPTION_ID"], "name": "PRIVATE_SUB_NAME", "tenantId": "PRIVATE_TENANT"}
        elif args[:2] == ("group", "exists"):
            source, payload = "group", True
        elif args[:3] == ("cognitiveservices", "account", "list"):
            source, payload = "accounts", [{"name": ACCOUNT, "kind": "AIServices", "location": "East US 2"}]
        elif args[:4] == ("cognitiveservices", "account", "deployment", "list"):
            source = "deployments"
            payload = [] if self.actual == {} else [{
                "name": self.actual["deploymentName"],
                "id": "/subscriptions/PRIVATE_ARM_ID",
                "sku": {"name": self.actual["sku"], "capacity": self.actual["capacity"]},
                "properties": {
                    "model": {
                        "name": self.actual["modelName"],
                        "format": self.actual["format"],
                        "version": self.actual["version"],
                    },
                    "versionUpgradeOption": self.actual["versionUpgradeOption"],
                    "provisioningState": self.actual["provisioningState"],
                    "endpoint": "https://PRIVATE_ENDPOINT.invalid",
                },
            }]
        elif args[:3] == ("cognitiveservices", "model", "list"):
            source, payload = "offerings", self.rows
        elif self.allow_quota and args[:3] == ("cognitiveservices", "usage", "list"):
            source, payload = "quota", [{
                "name": {"value": "OpenAI.GlobalStandard.example-model"}, "limit": 100, "currentValue": 0,
            }]
        else:
            raise AssertionError(f"Unexpected or non-read-only Azure operation: {args}")
        if source == self.fail:
            return subprocess.CompletedProcess(["az", *args], 1, "", "PRIVATE_RAW_AZURE_ERROR https://token.invalid?key=SECRET")
        if self.malformed and source == self.malformed[0]:
            return subprocess.CompletedProcess(["az", *args], 0, self.malformed[1], "")
        return subprocess.CompletedProcess(["az", *args], 0, json.dumps(payload), "")


class MultiRegionAzure(FakeAzure):
    def __init__(self, *, fail_west=False, allow_quota=False):
        super().__init__(
            [offered(instant(-1), lifecycle="Deprecated"), offered(instant(91), version="2")],
            allow_quota=allow_quota,
        )
        self.fail_west = fail_west

    def __call__(self, *args):
        result = super().__call__(*args)
        payload = json.loads(result.stdout)
        if args[:3] == ("cognitiveservices", "account", "list"):
            payload.append({
                "name": ACCOUNT.replace("eastus2", "westus"), "kind": "AIServices", "location": "westus"
            })
        elif args[:4] == ("cognitiveservices", "account", "deployment", "list"):
            if ACCOUNT.replace("eastus2", "westus") in args:
                if self.fail_west:
                    return subprocess.CompletedProcess(["az", *args], 1, "", "PRIVATE_REGIONAL_ERROR")
                payload[0]["name"] = payload[0]["name"].replace("eastus2", "westus")
            else:
                payload.append({**copy.deepcopy(payload[0]), "name": "retained-model"})
        return subprocess.CompletedProcess(["az", *args], 0, json.dumps(payload), "")


class RetirementCliTests(unittest.TestCase):
    def run_main(self, fake, *, report_mode=True, models=None, environment=None, public=None, workflow_args=False):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            catalog = temp / "models.json"
            catalog.write_text(json.dumps(models or CATALOG), encoding="utf-8")
            output = temp / "report"
            argv = ["check-model-availability.py"]
            if report_mode:
                argv += ["--retirement-report", str(output)]
            if workflow_args:
                workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
                collector = next(s for s in workflow["jobs"]["report"]["steps"] if s["name"] == "Collect retirement observations")
                command = collector["run"].split("python ", 1)[1].replace("\\\n", "")
                argv = shlex.split(command.replace("$RUNNER_TEMP/model-retirements", output.as_posix()))
            if public is not None:
                path = temp / "public.json"
                path.write_text(json.dumps(public), encoding="utf-8")
                argv += ["--public-evidence", str(path)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.object(AVAILABILITY, "MODELS_FILE", catalog),
                patch.object(AVAILABILITY, "_az", side_effect=fake),
                patch.object(AVAILABILITY, "datetime", FixedClock),
                patch.object(os, "environ", dict(READ_ENV if environment is None else environment)),
                patch.object(sys, "argv", argv),
                redirect_stdout(stdout), redirect_stderr(stderr),
            ):
                code = AVAILABILITY.main()
            artifacts = {path.name: path.read_text(encoding="utf-8") for path in output.iterdir()} if output.exists() else {}
            return code, artifacts, stdout.getvalue() + stderr.getvalue()

    def test_read_only_collector_and_workflow_command_emit_all_three_artifacts(self):
        fake = FakeAzure()
        code, artifacts, output = self.run_main(fake, workflow_args=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(artifacts), 3)
        self.assertIn("Retirement report: clear", output)
        self.assertEqual(len(fake.calls), 5)
        combined = output + "".join(artifacts.values())
        for secret in (ACCOUNT, "PRIVATE_SUB_NAME", "PRIVATE_TENANT", "private-subscription-id", "PRIVATE_ARM_ID", "PRIVATE_ENDPOINT"):
            self.assertNotIn(secret, combined)

    def test_each_failed_read_is_unknown_and_retains_no_raw_error_or_false_absence(self):
        self.assertEqual(self.run_main(FakeAzure())[0], 0)
        for source in ("context", "group", "accounts", "deployments", "offerings"):
            with self.subTest(source=source):
                fake = FakeAzure(fail=source)
                code, artifacts, output = self.run_main(fake)
                self.assertEqual(code, 2)
                data = json.loads(artifacts["model-retirements.json"])
                self.assertEqual(data["status"], "incomplete")
                self.assertTrue(any(s["status"] == "unavailable" for s in data["sources"]))
                self.assertNotIn("PRIVATE_RAW_AZURE_ERROR", output + "".join(artifacts.values()))
                self.assertNotIn("SECRET", output + "".join(artifacts.values()))
                if source in {"context", "group", "accounts", "deployments"}:
                    self.assertEqual(data["observations"][0]["inventory_state"], "unavailable")
                    self.assertEqual(data["observations"][0]["admission_decision"], "unknown")
                if source == "context":
                    self.assertEqual(len(fake.calls), 1)

    def test_malformed_success_responses_are_not_empty_clean_inventories(self):
        for source, payload in (
            ("context", "[]"), ("context", "{"),
            ("group", ""), ("group", "{}"),
            ("accounts", "{}"), ("accounts", "[null]"),
            ("deployments", ""), ("deployments", "[null]"),
            ("offerings", ""), ("offerings", "{}"), ("offerings", "[null]"),
            ("offerings", '[{"model":{"name":"example-model"}}]'),
            ("offerings", '[{"model":{"name":"a","format":"OpenAI","version":"1","skus":"bad"}}]'),
        ):
            with self.subTest(source=source, payload=payload):
                code, artifacts, _ = self.run_main(FakeAzure(malformed=(source, payload)))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(artifacts["model-retirements.json"])["status"], "incomplete")
        self.assertEqual(self.run_main(FakeAzure())[0], 0)

    def test_missing_or_wrong_subscription_context_prevents_followup_reads(self):
        fake = FakeAzure()
        code, artifacts, _ = self.run_main(fake, environment={})
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertEqual(json.loads(artifacts["model-retirements.json"])["unknown_observations"], 1)
        fake = FakeAzure(malformed=("context", '{"id":"wrong-subscription"}'))
        self.assertEqual(self.run_main(fake)[0], 2)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.run_main(FakeAzure())[0], 0)
        fake = FakeAzure()
        self.assertEqual(self.run_main(
            fake, environment={**READ_ENV, "AZURE_RESOURCE_GROUP": " ", "AZURE_ENV_NAME": " "}
        )[0], 2)
        self.assertEqual(fake.calls, [])

    def test_authoritative_missing_group_is_distinct_from_failed_inventory(self):
        missing = FakeAzure(malformed=("group", "false"))
        code, artifacts, _ = self.run_main(missing)
        self.assertEqual(code, 1)
        data = json.loads(artifacts["model-retirements.json"])
        self.assertEqual(data["observations"][0]["inventory_state"], "observed")
        self.assertIsNone(data["observations"][0]["deployed"])
        self.assertEqual(self.run_main(FakeAzure(fail="group"))[0], 2)

    def test_full_preflight_cannot_skip_the_date_guard_or_exact_existing_warnings(self):
        for date, actual, expected in (
            (instant(7), {}, 1),
            (instant(7, 1), {}, 0),
            (instant(-1), deployed(), 0),
        ):
            with self.subTest(date=date, actual=bool(actual)):
                fake = FakeAzure([offered(date)], actual=actual, allow_quota=True)
                code, _, output = self.run_main(fake, report_mode=False)
                self.assertEqual(code, expected)
                self.assertTrue(any(call[:3] == ("cognitiveservices", "usage", "list") for call in fake.calls))
                self.assertIn("model.skus[].deprecationDate", output)
                self.assertIn("NoAutoUpgrade", output)
                if actual:
                    self.assertIn("WARNING:", output)
                    self.assertIn("reconcile-warning", output)
                    self.assertIn("expired", output)

    def test_safe_target_upgrade_is_not_blocked_in_real_preflight(self):
        models = copy.deepcopy(CATALOG)
        models["catalog"][0]["deployments"][0]["version"] = "2"
        rows = [offered(instant(-1), lifecycle="Deprecated"), offered(instant(91), version="2")]
        code, _, output = self.run_main(FakeAzure(rows, allow_quota=True), report_mode=False, models=models)
        self.assertEqual(code, 0)
        self.assertIn("catalog=example-model@2", output)
        self.assertIn("deployed=example-model@1", output)
        self.assertIn("expired", output)
        rows[1] = offered(instant(7), version="2")
        self.assertEqual(
            self.run_main(FakeAzure(rows, allow_quota=True), report_mode=False, models=models)[0], 1
        )

    def test_unknown_dates_are_warnings_in_preflight_and_incomplete_in_reports(self):
        for date in (None, "garbage", "2026-09-09T00:00:00"):
            rows = [offered(date)]
            code, _, output = self.run_main(FakeAzure(rows, allow_quota=True), report_mode=False)
            self.assertEqual(code, 0)
            self.assertIn("coverage=unknown", output)
            self.assertNotIn("all 1 deployments are deployable", output)
            self.assertEqual(self.run_main(FakeAzure(rows))[0], 2)

    def test_other_format_lifecycle_does_not_override_the_actual_target(self):
        rows = [offered(instant(91)), offered(instant(-1), format="Anthropic", lifecycle="Deprecated")]
        self.assertEqual(
            self.run_main(FakeAzure(rows, allow_quota=True), report_mode=False)[0], 0
        )
        rows[0] = offered(instant(91), lifecycle="Deprecating")
        self.assertEqual(
            self.run_main(FakeAzure(rows, actual={}, allow_quota=True), report_mode=False)[0], 1
        )

    def test_bad_public_input_is_unavailable_without_echoing_raw_source(self):
        code, artifacts, output = self.run_main(
            FakeAzure(), public=[public_record(source_url=PUBLIC_URL + "?secret=PRIVATE_SECRET")]
        )
        self.assertEqual(code, 2)
        data = json.loads(artifacts["model-retirements.json"])
        self.assertEqual(data["public_comparison"], "unavailable")
        self.assertNotIn("PRIVATE_SECRET", output + "".join(artifacts.values()))
        self.assertEqual(self.run_main(FakeAzure(), public=[])[0], 0)

    def test_real_catalog_publisher_formats_admit_and_block_with_the_same_evidence(self):
        catalog = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        for publisher in sorted({model["format"] for model in catalog["catalog"]}):
            with self.subTest(publisher=publisher):
                models = copy.deepcopy(CATALOG)
                models["catalog"][0]["format"] = publisher
                rows = [offered(instant(-1), format=publisher)]
                fake = FakeAzure(rows, actual={}, allow_quota=True)
                environment = {**READ_ENV, "AI4IA_CLAUDE_ENABLED": "true"}
                self.assertEqual(self.run_main(
                    fake, models=models, environment=environment, report_mode=False
                )[0], 1)
                rows = [offered(instant(91), format=publisher)]
                fake = FakeAzure(rows, actual={}, allow_quota=True)
                self.assertEqual(self.run_main(
                    fake, models=models, environment=environment, report_mode=False
                )[0], 0)
                fake = FakeAzure(rows, actual=deployed(format=publisher))
                code, artifacts, _ = self.run_main(
                    fake, models=models, environment=environment,
                    public=[public_record(format=publisher, date=instant(91))],
                )
                self.assertEqual(code, 0)
                data = json.loads(artifacts["model-retirements.json"])
                self.assertEqual(data["observations"][0]["catalog"]["format"], publisher)
                self.assertTrue(any(
                    e["source"] == "public" for e in data["observations"][0]["catalog_evidence"]
                ))

    def test_failed_region_keeps_successful_old_and_out_of_catalog_deployments(self):
        models = copy.deepcopy(CATALOG)
        models["regions"]["westus"] = {}
        models["catalog"][0]["deployments"] = [
            {"region": region, "sku": "GlobalStandard", "version": "2", "capacity": 50}
            for region in ("eastus2", "westus")
        ]
        for failure, expected in ((False, 1), (True, 2)):
            with self.subTest(failure=failure):
                fake = MultiRegionAzure(fail_west=failure)
                code, artifacts, _ = self.run_main(fake, models=models)
                self.assertEqual(code, expected)
                self.assertEqual(len(fake.calls), 7)
                data = json.loads(artifacts["model-retirements.json"])
                east = [o for o in data["observations"] if o["region"] == "eastus2"]
                west = [o for o in data["observations"] if o["region"] == "westus"]
                self.assertEqual(len(east), 2)
                self.assertEqual(len(west), 1)
                self.assertTrue(all(o["inventory_state"] == "observed" for o in east))
                self.assertTrue(any(o["deployment_name"] == "retained-model" for o in east))
                self.assertTrue(all(
                    any(e["window"] == "expired" for e in o["deployed_evidence"])
                    for o in east
                ))
                self.assertEqual(west[0]["inventory_state"], "unavailable" if failure else "observed")
                if failure:
                    self.assertEqual(west[0]["drift"], ["inventory unavailable"])
                    self.assertNotIn("PRIVATE_REGIONAL_ERROR", "".join(artifacts.values()))
        self.assertEqual(self.run_main(
            MultiRegionAzure(allow_quota=True), models=models, report_mode=False
        )[0], 0)
        with self.assertRaises(SystemExit):
            self.run_main(
                MultiRegionAzure(fail_west=True, allow_quota=True), models=models, report_mode=False
            )

    def test_report_mode_rejects_writing_inside_the_checkout_before_azure_reads(self):
        with (
            patch.object(sys, "argv", ["check-model-availability.py", "--retirement-report", str(ROOT / "docs")]),
            patch.object(AVAILABILITY, "_az") as az,
            self.assertRaisesRegex(ValueError, "outside the source checkout"),
        ):
            AVAILABILITY.main()
        az.assert_not_called()

    def test_cli_timeout_and_response_budget_are_real_guards(self):
        with (
            patch.object(AVAILABILITY.shutil, "which", return_value="az"),
            patch.object(AVAILABILITY.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(["az"], 0, "[]", "")
            AVAILABILITY._az("cognitiveservices", "model", "list")
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            run.side_effect = subprocess.TimeoutExpired("az", 30)
            with self.assertRaisesRegex(SystemExit, "evidence is unavailable"):
                AVAILABILITY._az("cognitiveservices", "model", "list")
        control = subprocess.CompletedProcess(["az"], 0, "[]", "")
        self.assertEqual(AVAILABILITY._json_result(control, "fixture"), [])
        with patch.object(AVAILABILITY, "MAX_AZURE_RESPONSE_BYTES", 1), self.assertRaises(SystemExit):
            AVAILABILITY._json_result(control, "fixture")


BASH = find_bash()


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_only_main_manual_or_scheduled_collection_with_bounded_reader_permissions(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(set(triggers), {"workflow_dispatch", "schedule"})
        self.assertEqual(len(triggers["schedule"]), 1)
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        jobs = self.workflow["jobs"]
        self.assertEqual(
            jobs["activation"].get("permissions", self.workflow["permissions"]), {},
        )
        self.assertEqual(jobs["report"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(jobs["report"]["needs"], "activation")
        self.assertIn("needs.activation.outputs.enabled == 'true'", jobs["report"]["if"])
        self.assertIn("github.ref == 'refs/heads/main'", jobs["report"]["if"])
        self.assertLessEqual(jobs["report"]["timeout-minutes"], 15)
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], False)

    def test_read_only_workflow_has_no_mutation_actions_or_unbounded_artifact_upload(self):
        jobs = self.workflow["jobs"]
        steps = jobs["report"]["steps"]
        expected_actions = {"actions/checkout", "actions/setup-python", "azure/login", "actions/upload-artifact"}
        actual_actions = {s["uses"].split("@")[0] for s in steps if "uses" in s}
        self.assertEqual(actual_actions, expected_actions)
        for step in steps:
            self.assertNotIn("continue-on-error", step)
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertIs(step["with"]["persist-credentials"], False)
            if step.get("uses", "").startswith("azure/login@"):
                self.assertEqual(step["with"]["client-id"], "${{ vars.AI4IA_MODEL_RETIREMENT_CLIENT_ID }}")
        for name in (
            "Collect retirement observations", "Publish report summary",
            "Retain bounded report and generated documentation preview",
        ):
            self.assertEqual(next(s for s in steps if s["name"] == name)["if"], "${{ always() }}")
        upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["retention-days"], 30)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        paths = upload["with"]["path"].splitlines()
        self.assertEqual(len(paths), 3)
        self.assertFalse(any("*" in path for path in paths))
        self.assertTrue(all(path.startswith("${{ runner.temp }}/model-retirements/") for path in paths))
        run = "\n".join(s.get("run", "") for job in jobs.values() for s in job["steps"])
        for forbidden in ("azd ", "git push", "git commit", "check-resource-providers", "--register", "deployment create", "deployment delete", "sync-model-capacity"):
            self.assertNotIn(forbidden, run)

    def test_always_reported_quality_gate_runs_retirement_regressions(self):
        quality = yaml.safe_load((ROOT / ".github" / "workflows" / "quality.yml").read_text())
        self.assertIsNone(quality.get("on", quality.get(True))["pull_request"])
        run = "\n".join(s.get("run", "") for s in quality["jobs"]["script-tests"]["steps"])
        self.assertIn("scripts.tests.test_model_retirement", run)
        deploy = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
        self.assertIn("scripts/_model_retirement.py", deploy.get("on", deploy.get(True))["push"]["paths"])

    @unittest.skipIf(BASH is None, "bash is unavailable on this machine")
    def test_real_activation_script_has_paired_positive_and_negative_controls(self):
        step = self.workflow["jobs"]["activation"]["steps"][0]
        valid = {
            "REPORT_ENABLED": "true", "GITHUB_REF": "refs/heads/main",
            "REPORT_CLIENT_ID": "reader", "REPORT_TENANT_ID": "tenant",
            "REPORT_SUBSCRIPTION_ID": "subscription", "REPORT_RESOURCE_GROUP": "group",
            "REPORT_ENV_NAME": "prod", "REPORT_CAPACITY_PROFILE": "baseline",
            "REPORT_CLAUDE_ENABLED": "false", "DEPLOY_CLIENT_ID": "deployer",
        }
        cases = [
            ({}, 0, "enabled=true"),
            ({"REPORT_ENABLED": ""}, 0, "enabled=false"),
            ({"GITHUB_REF": "refs/heads/unreviewed"}, 1, None),
            ({"REPORT_CLIENT_ID": "DePlOyEr"}, 1, None),
            ({"REPORT_CAPACITY_PROFILE": "guessed"}, 1, None),
            ({"REPORT_CLAUDE_ENABLED": "guessed"}, 1, None),
            ({"REPORT_CAPACITY_PROFILE": "maximum", "REPORT_CLAUDE_ENABLED": "true"}, 0, "enabled=true"),
            ({"REPORT_CAPACITY_PROFILE": "production"}, 0, "enabled=true"),
            ({"REPORT_CAPACITY_PROFILE": "production", "REPORT_ENABLED": ""}, 0, "enabled=false"),
            ({"REPORT_CAPACITY_PROFILE": "production", "REPORT_CLIENT_ID": "DePlOyEr"}, 1, None),
        ]
        cases.extend(({name: ""}, 1, None) for name in valid if name.startswith("REPORT_") and name != "REPORT_ENABLED")
        for changes, code, expected_output in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output, summary = root / "output", root / "summary"
                result = subprocess.run(
                    [BASH, "--noprofile", "--norc", "-c", step["run"]],
                    env={
                        **os.environ, **valid, **changes,
                        "GITHUB_OUTPUT": output.as_posix(),
                        "GITHUB_STEP_SUMMARY": summary.as_posix(),
                    },
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                outputs = output.read_text() if output.exists() else ""
                if expected_output:
                    self.assertIn(expected_output, outputs)
                else:
                    self.assertNotIn("enabled=true", outputs)
                if expected_output == "enabled=false":
                    self.assertIn("disabled/unobserved", summary.read_text())


if __name__ == "__main__":
    unittest.main()
