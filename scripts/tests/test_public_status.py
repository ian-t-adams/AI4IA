"""Synthetic private observations must never cross either public publisher."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import public_status, uptime
from scripts.http_contract import HttpError, stamp
from scripts.tests.test_uptime import (
    ENVIRONMENT,
    NOW,
    FakeTransport,
    replies,
    report,
    response,
)

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = (
    "11111111-2222-4333-8444-555555555555", "rg-private-fixture",
    "private-resource-unique-suffix", "eastus2",
    "https://private-origin.example.test",
)


def snapshots() -> tuple[dict, dict]:
    secret = " ".join(PRIVATE)
    inv = {
        "generatedAt": stamp(NOW), "subscription": PRIVATE[0], "resourceGroup": PRIVATE[1],
        "resources": [{
            "id": f"/subscriptions/{PRIVATE[0]}/resourceGroups/{PRIVATE[1]}/providers/example",
            "name": PRIVATE[2], "location": PRIVATE[3],
            "type": "microsoft.storage/storageaccounts",
            "label": secret, "group": secret, "availability": "Available",
            "provisioningState": "Succeeded", "healthReported": True, "extra": secret,
        }],
    }
    status = {
        "generatedAt": stamp(NOW), "subscription": PRIVATE[0], "resourceGroup": PRIVATE[1],
        "healthSource": {"status": "available", "note": secret, "providerState": secret},
        "summary": {
            "total": 1, "healthy": 1, "provisioned": 0, "degraded": 0,
            "unavailable": 0, "endpointsUp": 4, "endpointsTot": 4, "extra": secret,
        },
        "endpoints": [
            {"name": secret, "url": PRIVATE[4], "state": "up", "ok": True,
             "outcome": "reachable", "httpStatus": 200,
             "observedAt": stamp(NOW), "latencyMs": 12, "note": secret},
            {"name": secret, "url": PRIVATE[4], "state": "up", "ok": True,
             "outcome": "reachable", "httpStatus": 401,
             "observedAt": stamp(NOW), "latencyMs": 12, "note": secret},
            *[
                {"name": secret, "url": PRIVATE[4], "state": "up", "ok": True,
                 "outcome": "healthy", "httpStatus": 200,
                 "observedAt": stamp(NOW), "latencyMs": 12, "note": secret}
                for _ in range(2)
            ],
        ],
    }
    return inv, status


class PublicStatusTests(unittest.TestCase):
    def assert_private_absent(self, text: str):
        for value in PRIVATE:
            self.assertNotIn(value, text)

    def test_projection_is_allowlisted_idempotent_and_preserves_positive_evidence(self):
        inv, health = snapshots()
        projected = public_status.inventory(inv), public_status.status(health)
        self.assert_private_absent(json.dumps(projected))
        self.assertEqual(projected[0]["resources"][0]["label"], "Storage Account")
        self.assertEqual(projected[0]["resources"][0]["state"], "healthy")
        self.assertEqual(projected[1]["summary"]["healthy"], 1)
        self.assertEqual(projected[1]["endpoints"][3]["state"], "up")
        self.assertEqual(projected[1]["endpoints"][1]["state"], "up")
        self.assertEqual(public_status.inventory(projected[0]), projected[0])
        self.assertEqual(public_status.status(projected[1]), projected[1])
        self.assertIn(PRIVATE[2], json.dumps(inv), "private input control did not contain the fixture")
        self.assertIn(PRIVATE[4], json.dumps(health))

    def test_unknown_degraded_and_source_failure_remain_distinct(self):
        inv, health = snapshots()
        for availability, expected in (("Unknown", "provisioned"), ("Degraded", "degraded"),
                                       ("Unavailable", "unavailable"), ("Available", "healthy")):
            inv["resources"][0]["availability"] = availability
            self.assertEqual(public_status.inventory(inv)["resources"][0]["state"], expected)
        health["healthSource"]["status"] = "unavailable"
        health["endpoints"][0].update(state="down", ok=False, outcome="http_error", httpStatus=503)
        health["endpoints"][1].update(state="unknown", outcome="network_unavailable")
        result = public_status.status(health)
        self.assertEqual(result["healthSource"]["status"], "unavailable")
        self.assertEqual([row["state"] for row in result["endpoints"]], ["down", "unknown", "up", "up"])

    def test_unknown_types_and_free_text_are_never_passed_through(self):
        inv, health = snapshots()
        inv["resources"][0].update(type=PRIVATE[2], availability=PRIVATE[2],
                                   provisioningState=PRIVATE[2])
        health["endpoints"][0]["outcome"] = PRIVATE[2]
        self.assert_private_absent(json.dumps((public_status.inventory(inv), public_status.status(health))))
        self.assertEqual(public_status.inventory(inv)["resources"][0]["label"], "Azure service")
        self.assertEqual(public_status.status(health)["endpoints"][0]["state"], "unknown")

    def test_inventory_positive_health_requires_report_evidence_at_public_boundary(self):
        from scripts.tests.test_pages_status_refresh import PagesStatusRefreshTests

        renderer = PagesStatusRefreshTests()
        for reported in (True, False, None):
            with self.subTest(reported=reported), tempfile.TemporaryDirectory() as temporary:
                inv, health = snapshots()
                inv["resources"][0]["healthReported"] = reported
                site = Path(temporary) / "site"
                shutil.copytree(ROOT / "site", site)
                public_status.write_snapshots(site / "data", inv, health)
                public_status.check_site(site)
                projected = public_status.assignment(
                    (site / "data" / "inventory.js").read_bytes(), "AI4IA_INVENTORY",
                )
                self.assertEqual(public_status.inventory(projected), projected)
                expected = "healthy" if reported is True else "provisioned"
                self.assertEqual(projected["resources"][0]["state"], expected)
                for rows in (inv["resources"], projected["resources"]):
                    rendered = renderer._render_status(resources=rows)
                    self.assertIn(f">{expected}</span>", rendered["resources"])
                    self.assertIn(
                        f'<div class="n">{int(reported is True)}</div>'
                        '<div class="l">Health reported available</div>',
                        rendered["stats"],
                    )
                    if reported is not True:
                        self.assertNotIn(">healthy</span>", rendered["resources"])
                        self.assertNotIn(">Available<", rendered["resources"])
                for availability, provisioning, state in (
                    ("Degraded", "Succeeded", "degraded"),
                    ("Unavailable", "Succeeded", "unavailable"),
                    ("Available", "Failed", "degraded"),
                ):
                    inv["resources"][0].update(
                        availability=availability, provisioningState=provisioning,
                    )
                    self.assertEqual(public_status.inventory(inv)["resources"][0]["state"], state)

    def test_inconsistent_endpoint_evidence_fails_closed_and_summary_counts_match(self):
        _, health = snapshots()
        inconsistent = health["endpoints"][0]
        inconsistent.update(state="up", ok=False, outcome="http_error", httpStatus=503)
        result = public_status.status(health)
        self.assertEqual(result["endpoints"][0]["state"], "unknown")
        self.assertFalse(result["endpoints"][0]["ok"])
        self.assertEqual(result["summary"]["endpointsUp"], 3)
        self.assertEqual(result["summary"]["endpointsTot"], 4)
        serialized = json.dumps(result)
        self.assertNotIn("private-resource-unique-suffix", serialized)
        self.assertEqual(public_status.status(result), result)

        for state, ok, outcome, status in (
            ("up", True, "reachable", 503),
            ("down", False, "reachable", 401),
            ("up", True, "healthy", 302),
        ):
            with self.subTest(state=state, outcome=outcome, status=status):
                _, bad = snapshots()
                bad["endpoints"][0].update(state=state, ok=ok, outcome=outcome, httpStatus=status)
                self.assertEqual(public_status.status(bad)["endpoints"][0]["state"], "unknown")

    def test_safe_api_target_outcomes_remain_distinct_without_provider_notes(self):
        outcomes = ("private_ingress", "target_ambiguous", "auth_required", "unexpected_redirect")
        http_statuses = (0, 0, 401, 302)
        for outcome, http_status in zip(outcomes, http_statuses, strict=True):
            with self.subTest(outcome=outcome):
                _, health = snapshots()
                health["endpoints"][2].update(
                    state="unknown", ok=False, outcome=outcome, httpStatus=http_status,
                    note=PRIVATE[2],
                )
                result = public_status.status(health)
                self.assertEqual(result["endpoints"][2]["outcome"], outcome)
                self.assertEqual(result["endpoints"][2]["state"], "unknown")
                self.assert_private_absent(json.dumps(result))

    def test_endpoint_matrix_survives_artifact_projection_and_rendering(self):
        from scripts.tests.test_pages_status_refresh import PagesStatusRefreshTests

        renderer = PagesStatusRefreshTests()
        cases = [
            (0, "up", True, "reachable", code, stamp(NOW), "up")
            for code in (200, 204, 302, 401, 403)
        ] + [
            (index, "up", True, "healthy", 200, stamp(NOW), "up") for index in (2, 3)
        ] + [
            (0, "down", False, "http_error", 503, stamp(NOW), "down"),
            (2, "down", False, "http_error", 503, stamp(NOW), "down"),
            (3, "down", False, "persistence_unavailable", 503, stamp(NOW), "down"),
            (0, "up", True, "reachable", 503, stamp(NOW), "unknown"),
            (2, "up", True, "healthy", 302, stamp(NOW), "unknown"),
            (0, "down", False, "http_error", 0, stamp(NOW), "unknown"),
            (0, "up", True, "reachable", 200, None, "unknown"),
            (2, "up", True, "healthy", 200, None, "unknown"),
            (0, "down", False, "http_error", 503, None, "unknown"),
            (3, "down", False, "persistence_unavailable", 503, None, "unknown"),
        ] + [
            (2, "down", False, "http_error", code, stamp(NOW), "unknown")
            for code in (302, 401, 403)
        ] + [
            (2, "unknown", False, outcome, code, stamp(NOW), "unknown")
            for outcome, code in (
                ("auth_required", 401), ("auth_required", 403), ("unexpected_redirect", 302),
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            shutil.copytree(ROOT / "site", site)
            for index, state, ok, outcome, code, observed, expected in cases:
                with self.subTest(index=index, state=state, outcome=outcome, code=code, observed=observed):
                    inv, health = snapshots()
                    health["endpoints"][index].update(
                        state=state, ok=ok, outcome=outcome, httpStatus=code, observedAt=observed,
                    )
                    public_status.write_snapshots(site / "data", inv, health)
                    public_status.check_site(site)
                    projected = public_status.assignment(
                        (site / "data" / "status.js").read_bytes(), "AI4IA_STATUS",
                    )
                    self.assertEqual(public_status.status(projected), projected)
                    row = projected["endpoints"][index]
                    self.assertEqual(row["state"], expected)
                    expected_up = 4 if expected == "up" else 3
                    self.assertEqual(projected["summary"]["endpointsUp"], expected_up)
                    rendered = renderer._render_status(endpoints=projected["endpoints"])
                    self.assertIn(f"{expected_up}/4", rendered["stats"])
                    card = rendered["endpoints"].split('<div class="card">')[index + 1]
                    raw = renderer._render_status(endpoints=[{
                        **health["endpoints"][index], "kind": row["kind"], "name": row["name"],
                    }])
                    if expected == "unknown":
                        self.assertIn("not established", card)
                        self.assertNotIn("Public ingress returned an expected response.", card)
                        self.assertNotIn("Expected API health response.", card)
                        self.assertNotIn("Public ingress returned an expected response.", raw["endpoints"])
                        self.assertNotIn("Expected API health response.", raw["endpoints"])
                        self.assertNotIn('class="badge bad"', raw["endpoints"])
                    else:
                        self.assertIn(
                            'class="badge ok"' if expected == "up" else 'class="badge bad"', card,
                        )

    def test_legacy_uptime_urls_are_migrated_without_losing_alerts_or_scope(self):
        legacy = report("down", report("down", report("down")))
        legacy["version"] = 1
        for row, (identifier, _, path) in zip(legacy["endpoints"], uptime.ENDPOINTS, strict=True):
            del row["targetKey"]
            row["url"] = f"https://{'api' if identifier.startswith('api_') else identifier}.example.test{path}"
        migrated = uptime.parse_report(json.dumps(legacy).encode())
        self.assertEqual(migrated["version"], 2)
        self.assertNotIn("example.test", json.dumps(migrated))
        self.assertTrue(all(row["alerting"] for row in migrated["endpoints"]))
        current = report("up", migrated)
        self.assertTrue(all(row["transition"] == "recovered" for row in current["endpoints"]))
        self.assertEqual(current["history"], "available")
        for row in legacy["endpoints"]:
            row["url"] = "https://replacement.example.test" + next(
                path for identifier, _, path in uptime.ENDPOINTS if identifier == row["id"]
            )
        changed = report("down", uptime.parse_report(json.dumps(legacy).encode()))
        self.assertEqual(changed["history"], "scope_changed")
        self.assertTrue(all(row["failureStreak"] == 1 for row in changed["endpoints"]))

    def test_actual_site_and_final_artifact_boundary(self):
        public_status.check_site(ROOT / "site")
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            shutil.copytree(ROOT / "site", site)
            inv, health = snapshots()
            public_status.write_snapshots(site / "data", inv, health)
            public_status.check_site(site)
            for name, value in (("inventory", inv), ("status", health)):
                path = site / "data" / f"{name}.js"
                original = path.read_bytes()
                path.write_text(f"window.AI4IA_{name.upper()} = {json.dumps(value)};\n")
                with self.assertRaises(HttpError):
                    public_status.check_site(site)
                path.write_bytes(original)
            (site / "extra.html").write_text(PRIVATE[0])
            result = subprocess.run(
                [sys.executable, "-m", "scripts.public_status", "--check-site", str(site)],
                capture_output=True, text=True, cwd=ROOT, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assert_private_absent(result.stdout + result.stderr)

    def test_workflow_private_inputs_are_masked_before_any_runner_step(self):
        for name in ("pages", "uptime"):
            caller = yaml.safe_load((ROOT / ".github" / "workflows" / f"{name}.yml").read_text())
            call = caller["jobs"]["publish"]
            self.assertEqual(call["uses"], f"./.github/workflows/{name}-publish.yml")
            self.assertNotIn("env", caller)
            self.assertNotIn("env", call)
            self.assertNotIn("with", call)
            protected = yaml.safe_load((ROOT / call["uses"][2:]).read_text())
            declared = protected.get("on", protected.get(True))["workflow_call"]["secrets"]
            self.assertEqual(set(declared), set(call["secrets"]))
            self.assertEqual(call["secrets"], {key: "${{ vars." + key + " }}" for key in declared})
            serialized = json.dumps(protected)
            self.assertNotIn("${{ vars.", serialized)
            self.assertNotIn("secrets: inherit", serialized)
            for job in protected["jobs"].values():
                steps = job["steps"]
                upload = next((i for i, step in enumerate(steps)
                               if step.get("uses", "").startswith("actions/upload-pages-artifact@")), None)
                if upload is not None:
                    check = next(i for i, step in enumerate(steps)
                                 if step.get("run") == "python -m scripts.public_status --check-site site")
                    self.assertLess(check, upload)
        pages = yaml.safe_load((ROOT / ".github" / "workflows" / "pages-publish.yml").read_text())
        self.assertEqual(pages["jobs"]["build"]["env"]["AZURE_CORE_OUTPUT"], "none")
        refresh = next(step for step in pages["jobs"]["build"]["steps"]
                       if step.get("name") == "Refresh deployment status snapshot")
        self.assertIn("-Public", refresh["run"])


class PublicCarryTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_sanitizes_legacy_snapshots_and_does_not_log_private_targets(self):
        inv, health = snapshots()
        FakeTransport.sent = []
        FakeTransport.replies = replies()
        for name, value in (("inventory", inv), ("status", health)):
            FakeTransport.replies[f"/AI4IA/data/{name}.js"] = response(
                f"window.AI4IA_{name.upper()} = {json.dumps(value)};".encode(),
                content_type="text/javascript",
            )
        with tempfile.TemporaryDirectory() as temporary, patch.object(uptime, "Transport", FakeTransport), \
                patch.object(uptime, "now", return_value=NOW), redirect_stdout(io.StringIO()) as logs:
            directory = Path(temporary)
            await uptime.run("observe", directory, {
                **ENVIRONMENT, "AI4IA_UPTIME_API_URL": PRIVATE[4],
            })
            exported = logs.getvalue() + "".join(path.read_text() for path in directory.iterdir())
            PublicStatusTests().assert_private_absent(exported)
            public_inv = public_status.assignment((directory / "inventory.js").read_bytes(), "AI4IA_INVENTORY")
            self.assertEqual(public_inv["resources"][0]["state"], "healthy")
            self.assertTrue(any(PRIVATE[4] in url for _, url, _ in FakeTransport.sent),
                            "positive control: the actual private target was not probed")
