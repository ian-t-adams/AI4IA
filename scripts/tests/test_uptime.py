"""The read-only monitor recovers without approval and never equates unknown with health."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import aiohttp
import yaml

from scripts import uptime
from scripts.canaries.contracts import CanaryError, stamp
from scripts.canaries.transport import Response
from scripts.tests import test_gating_workflows as gating

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 14, 20, tzinfo=timezone.utc)
ENVIRONMENT = {
    "AI4IA_WEB_CUSTOM_DOMAIN": "web.example.test",
    "AI4IA_PROXY_CUSTOM_DOMAIN": "proxy.example.test",
    "AI4IA_UPTIME_API_URL": "https://api.example.test",
}


def response(value: object, *, status: int = 200, content_type: str = "application/json") -> Response:
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return Response(status, body, 0.01, content_type)


def rows(state: uptime.State = "up") -> list[uptime.Observation]:
    return [
        uptime.Observation(
            id=identifier,
            url=f"https://{'api' if identifier.startswith('api_') else identifier}.example.test{path}",
            state=state, reason="healthy" if state == "up" else "http_error",
            observedAt=stamp(NOW), httpStatus=200 if state == "up" else 503,
            latencyMs=10, failureStreak=0, alerting=False, transition="none",
        )
        for identifier, _, path in uptime.ENDPOINTS
    ]


def report(state: uptime.State = "up", previous: uptime.Report | None = None) -> uptime.Report:
    return uptime.finish(rows(state), previous, NOW)


class FakeTransport:
    replies: dict[str, Response | CanaryError] = {}
    sent: list[tuple[str, str, dict]] = []
    allowed: set[str] = set()

    def __init__(self, allowed: set[str]):
        type(self).allowed = allowed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def request(self, method: str, url: str, **kwargs) -> Response:
        type(self).sent.append((method, url, kwargs))
        reply = type(self).replies.get(urlsplit(url).path, response({}, status=404))
        if isinstance(reply, CanaryError):
            raise reply
        return reply


def replies() -> dict[str, Response | CanaryError]:
    return {
        "/": response(b"<!doctype html><html>App</html>", content_type="text/html"),
        "/liveness": response(b"OK\n", content_type="text/plain"),
        "/health/live": response({"status": "ok"}),
        "/health/ready": response({"status": "ok", "stage": "session_store"}),
        "/AI4IA/data/inventory.js": response(
            ("// generated\nwindow.AI4IA_INVENTORY = " + json.dumps({
                "generatedAt": stamp(NOW - timedelta(hours=2)), "resources": [{"name": "observed"}],
            }) + ";\n").encode(), content_type="text/javascript",
        ),
        "/AI4IA/data/status.js": response(
            ("window.AI4IA_STATUS = " + json.dumps({
                "generatedAt": stamp(NOW - timedelta(hours=2)), "summary": {"total": 1},
            }) + ";\n").encode(), content_type="text/javascript",
        ),
    }


class UptimeStateTests(unittest.TestCase):
    def test_fires_at_exactly_three_failures_and_does_not_repeat(self):
        previous = None
        for attempt in range(1, 7):
            previous = report("down", previous)
            for row in previous["endpoints"]:
                self.assertEqual(row["failureStreak"], min(attempt, 3))
                self.assertEqual(row["alerting"], attempt >= 3)
                self.assertEqual(row["transition"], "firing" if attempt == 3 else "none")
        self.assertEqual(previous["overall"], "down")

    def test_same_incident_recovers_on_the_first_pass_and_can_fire_again(self):
        previous = report("down", report("down", report("down")))
        current = report("up", previous)
        self.assertTrue(all(row["transition"] == "recovered" for row in current["endpoints"]))
        self.assertTrue(all(row["failureStreak"] == 0 and not row["alerting"] for row in current["endpoints"]))
        self.assertEqual(current["overall"], "up")
        again = report("down", report("down", report("down", current)))
        self.assertTrue(all(row["transition"] == "firing" for row in again["endpoints"]))

    def test_unknown_is_nonpassing_but_is_not_a_confirmed_outage(self):
        observed = rows("unknown")
        for row in observed:
            row["reason"], row["httpStatus"] = "deadline", None
        current = uptime.finish(observed, None, NOW)
        self.assertEqual(current["overall"], "unknown")
        self.assertTrue(all(row["failureStreak"] == 1 for row in current["endpoints"]))
        self.assertFalse(any(row["alerting"] for row in current["endpoints"]))

    def test_missing_stale_and_changed_history_do_not_stop_current_checks(self):
        active = report("down", report("down", report("down")))
        for missing in (None, {**active, "checkedAt": stamp(NOW - timedelta(minutes=46))}):
            with self.subTest(missing=missing is None):
                current = uptime.finish(rows(), missing, NOW)
                self.assertEqual(current["overall"], "up")
                self.assertIn(current["history"], ("unavailable", "stale"))
                failed = uptime.finish(rows("down"), missing, NOW)
                self.assertTrue(all(row["failureStreak"] == 1 for row in failed["endpoints"]))
        changed = rows("down")
        changed[0]["url"] = "https://replacement.example.test/"
        current = uptime.finish(changed, active, NOW)
        self.assertEqual(current["history"], "scope_changed")
        self.assertEqual(current["endpoints"][0]["failureStreak"], 1)
        self.assertEqual(current["endpoints"][1]["failureStreak"], 3)

    def test_history_window_includes_45_minutes_but_refuses_future_and_expired(self):
        active = report("down", report("down", report("down")))
        for age, expected in ((2700, "available"), (2701, "stale"), (-1, "stale")):
            previous = {**active, "checkedAt": stamp(NOW - timedelta(seconds=age))}
            current = uptime.finish(rows("down"), previous, NOW)
            self.assertEqual(current["history"], expected)
            self.assertEqual(current["endpoints"][0]["failureStreak"], 3 if expected == "available" else 1)

    def test_report_roundtrip_and_malformed_history_are_paired(self):
        valid = report()
        self.assertEqual(uptime.parse_report(json.dumps(valid).encode()), valid)
        for field, value in (
            ("id", "wrong"), ("reason", []), ("url", "https://[invalid/"),
            ("url", "https://user:password@api.example.test/health/live"),
            ("url", "https://127.0.0.1/"), ("httpStatus", True),
            ("observedAt", None), ("failureStreak", True), ("alerting", True),
            ("latencyMs", 30001), ("latencyMs", 1.1), ("state", "not-a-state"),
        ):
            with self.subTest(field=field, value=value):
                malformed = copy.deepcopy(valid)
                malformed["endpoints"][0][field] = value
                with self.assertRaises(CanaryError):
                    uptime.parse_report(json.dumps(malformed).encode())
        with self.assertRaises(CanaryError):
            uptime.parse_report(b'{"version":1,"version":1}')

    def test_targets_require_public_https_origins_not_credentials_or_paths(self):
        self.assertEqual(uptime.origins(ENVIRONMENT)["api"], "https://api.example.test")
        for invalid in ("", "http://api.example.test", "https://user:pass@api.example.test",
                        "https://api.example.test/path", "https://api.example.test?token=value",
                        "https://127.0.0.1", "https://api.local", "https://api.example.test:444"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(uptime.origins({**ENVIRONMENT, "AI4IA_UPTIME_API_URL": invalid})["api"])


class UptimeProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeTransport.sent = []
        FakeTransport.replies = replies()
        self.client = FakeTransport(set())

    async def test_all_four_real_probe_contracts_have_positive_controls(self):
        for identifier, _, path in uptime.ENDPOINTS:
            with self.subTest(identifier=identifier), patch.object(uptime, "now", return_value=NOW):
                row = await uptime.probe(self.client, identifier, "https://api.example.test" + path)
                self.assertEqual((row["state"], row["reason"]), ("up", "healthy"))
                self.assertEqual(row["observedAt"], stamp(NOW))
                self.assertIsNotNone(row["latencyMs"])
        self.assertEqual(len(FakeTransport.sent), 4)
        for method, _, arguments in FakeTransport.sent:
            self.assertEqual(method, "GET")
            self.assertEqual(set(arguments), {"timeout", "limit"})
            self.assertEqual(arguments["timeout"], 20)

    async def test_readiness_checks_storage_and_does_not_accept_auth_or_bad_json(self):
        for failed in (
            response({"status": "ok"}),
            response({"status": "ok", "stage": "other"}),
            response({"status": "unavailable", "stage": "session_store"}, status=503),
            response({}, status=401), response({}, status=403), response({}, status=302),
            response(b'{"status":"ok","status":"bad"}'),
            response(b'{"status":"ok","stage":"session_store"}', content_type="text/html"),
            CanaryError("redirect_rejected"), CanaryError("response_too_large"), CanaryError("deadline"),
        ):
            with self.subTest(failed=failed):
                FakeTransport.replies["/health/ready"] = failed
                row = await uptime.probe(self.client, "api_ready", "https://api.example.test/health/ready")
                self.assertNotEqual(row["state"], "up")
                self.assertNotEqual(row["reason"], "healthy")
        FakeTransport.replies["/health/ready"] = replies()["/health/ready"]
        self.assertEqual((await uptime.probe(self.client, "api_ready", "https://api.example.test/health/ready"))["state"], "up")

    async def test_proxy_requires_its_liveness_payload_not_a_login_page(self):
        FakeTransport.replies["/liveness"] = response(b"<html>Sign in</html>", content_type="text/html")
        row = await uptime.probe(self.client, "proxy", "https://proxy.example.test/liveness")
        self.assertEqual(row["state"], "unknown")
        FakeTransport.replies["/liveness"] = replies()["/liveness"]
        self.assertEqual((await uptime.probe(self.client, "proxy", "https://proxy.example.test/liveness"))["state"], "up")

    async def test_unconfigured_target_never_sends_a_request_or_invents_timing(self):
        row = await uptime.probe(self.client, "api_ready", None)
        self.assertEqual(row["reason"], "invalid_configuration")
        self.assertIsNone(row["observedAt"])
        self.assertIsNone(row["latencyMs"])
        self.assertEqual(FakeTransport.sent, [])

    async def test_unavailable_or_corrupt_history_does_not_prevent_four_checks(self):
        for history in (
            response({}, status=404), CanaryError("network_unavailable"),
            response(b"{not-json"), response({"version": 1}),
            response({**report(), "checkedAt": stamp(NOW + timedelta(minutes=5))}),
        ):
            FakeTransport.replies = {**replies(), "/AI4IA/data/uptime.json": history}
            FakeTransport.sent = []
            with tempfile.TemporaryDirectory() as temporary, patch.object(uptime, "Transport", FakeTransport), \
                    patch.object(uptime, "now", return_value=NOW), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(io.StringIO()):
                await uptime.run("observe", Path(temporary), ENVIRONMENT)
                current = uptime.parse_report((Path(temporary) / "uptime.json").read_bytes())
                self.assertEqual(current["overall"], "up")
                self.assertNotEqual(current["history"], "available")
                self.assertEqual(len(FakeTransport.sent), 7)
                self.assertTrue(all(item[0] == "GET" for item in FakeTransport.sent))
                self.assertEqual({urlsplit(item[1]).path for item in FakeTransport.sent}, {
                    "/AI4IA/data/uptime.json", "/AI4IA/data/inventory.js", "/AI4IA/data/status.js",
                    "/", "/liveness", "/health/live", "/health/ready",
                })
                inventory = (Path(temporary) / "inventory.js").read_text()
                self.assertIn(stamp(NOW - timedelta(hours=2)), inventory)
                self.assertNotIn(stamp(NOW), inventory)

    async def test_snapshot_failure_never_substitutes_a_healthy_committed_seed(self):
        FakeTransport.replies["/AI4IA/data/inventory.js"] = response(b"window.AI4IA_INVENTORY = alert('no');")
        with tempfile.TemporaryDirectory() as temporary, patch.object(uptime, "now", return_value=NOW), \
                redirect_stdout(io.StringIO()) as logs:
            directory = Path(temporary)
            (directory / "inventory.js").write_text("committed healthy seed")
            await uptime.carry_snapshots(self.client, directory)
            self.assertEqual((directory / "inventory.js").read_text(), "window.AI4IA_INVENTORY = null;\n")
            self.assertIn("snapshot unavailable", logs.getvalue())
            self.assertIn('"total": 1', (directory / "status.js").read_text())

    async def test_azure_publisher_carries_history_without_running_health_probes(self):
        current = report()
        FakeTransport.replies["/AI4IA/data/uptime.json"] = response(current)
        with tempfile.TemporaryDirectory() as temporary, patch.object(uptime, "Transport", FakeTransport), \
                patch.object(uptime, "now", return_value=NOW):
            directory = Path(temporary)
            await uptime.run("carry", directory, ENVIRONMENT)
            self.assertEqual(uptime.parse_report((directory / "uptime.json").read_bytes()), current)
            self.assertEqual(len(FakeTransport.sent), 1)
            self.assertEqual(urlsplit(FakeTransport.sent[0][1]).path, "/AI4IA/data/uptime.json")
            self.assertFalse((directory / "inventory.js").exists())

    async def test_collection_persists_firing_and_recovery_without_private_data(self):
        previous = report("down", report("down", report("down")))
        previous["endpoints"][-1]["failureStreak"] = 2
        previous["endpoints"][-1]["alerting"] = False
        previous["endpoints"][-1]["transition"] = "none"
        FakeTransport.replies = {
            **replies(), "/health/ready": response({}, status=503),
            "/AI4IA/data/uptime.json": response(previous),
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(uptime, "Transport", FakeTransport), \
                patch.object(uptime, "now", return_value=NOW), redirect_stdout(io.StringIO()) as logs:
            root = Path(temporary)
            with patch.dict(os.environ, {
                "GITHUB_STEP_SUMMARY": str(root / "summary"), "GITHUB_OUTPUT": str(root / "output"),
                "AI4IA_CANARY_CONFIG": "private" + "-fixture",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "do-not" + "-export",
            }, clear=True):
                await uptime.run("observe", root, ENVIRONMENT)
            current = uptime.parse_report((root / "uptime.json").read_bytes())
            self.assertEqual(current["overall"], "down")
            self.assertEqual(current["endpoints"][-1]["transition"], "firing")
            self.assertEqual((root / "output").read_text(), "firing=true\nrecovered=true\n")
            exported = (root / "summary").read_text() + (root / "uptime.json").read_text() + logs.getvalue()
            self.assertNotIn("private-fixture", exported)
            self.assertNotIn("do-not-export", exported)
            self.assertNotIn('"status": "ok"', exported)
            self.assertIn("Collection success is not application health", exported)


class UptimeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "uptime.yml").read_text())

    def test_monitor_is_main_only_every_15_minutes_and_has_no_model_authority(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(triggers["schedule"], [{"cron": "7,22,37,52 * * * *"}])
        self.assertNotIn("pull_request", triggers)
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"], {"group": "pages", "cancel-in-progress": False})
        observer = self.workflow["jobs"]["observe"]
        self.assertIn("refs/heads/main", observer["if"])
        self.assertEqual(observer["permissions"], {"contents": "read"})
        self.assertNotIn("environment", observer)
        for step in observer["steps"]:
            self.assertNotRegex(json.dumps(step), r"AZURE_CLIENT_ID|CANARY_CONFIG|OIDC|Authorization|scripts\.canaries observe")
            self.assertNotRegex(step.get("run", ""), r"\baz(?:d)?\b|gh api|/api/sessions|/api/chat|voice")
        gating.WorkflowPermissionBoundaryTests().assert_permission_boundary(self.workflow)

    def test_publication_precedes_transition_alert_and_never_repeats_a_known_incident(self):
        deployment = self.workflow["jobs"]["deploy"]
        steps = deployment["steps"]
        deploy_index = next(index for index, step in enumerate(steps) if step.get("id") == "deployment")
        alert = next(step for step in steps if step["name"] == "Alert once for a new incident")
        self.assertGreater(steps.index(alert), deploy_index)
        self.assertEqual(alert["if"], "needs.observe.outputs.firing == 'true'")
        self.assertIn("exit 1", alert["run"])
        recovery = next(step for step in steps if step["name"] == "Record automatic recovery")
        self.assertEqual(recovery["if"], "needs.observe.outputs.recovered == 'true'")
        self.assertNotIn("exit 1", recovery["run"])
        page_workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "pages.yml").read_text())
        self.assertEqual(page_workflow["concurrency"], self.workflow["concurrency"])
        names = [step["name"] for step in page_workflow["jobs"]["build"]["steps"]]
        self.assertLess(names.index("Refresh deployment status snapshot"),
                        names.index("Preserve independently collected uptime observations"))
        self.assertLess(names.index("Preserve independently collected uptime observations"), names.index("Upload site artifact"))

    def test_fresh_import_has_no_api_sdk_or_credential_dependency(self):
        # Isolated Python omits the quality job's --user HTTP dependency install.
        http_site = Path(aiohttp.__file__).resolve().parent.parent
        script = f"""
import importlib
import importlib.abc
import sys
sys.path.insert(0, {str(http_site)!r})
sys.path.insert(0, {str(ROOT)!r})
class DenyApi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('ai4ia_api', 'azure', 'pydantic', 'fastapi', 'durabletask'):
            raise RuntimeError('API runtime forbidden')
sys.meta_path.insert(0, DenyApi())
try:
    importlib.import_module('pydantic')
except RuntimeError:
    pass
else:
    raise AssertionError('The dependency-denial control did not execute')
import scripts.uptime
assert not any(name.startswith('ai4ia_api') for name in sys.modules)
print('standalone HTTP runtime')
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "standalone HTTP runtime")


class UptimePortalTests(unittest.TestCase):
    def render(self, current: uptime.Report | None, *, bad_fetch=False, advance=0, oversized=False) -> dict:
        source = json.dumps(str(ROOT / "site" / "assets" / "app.js"))
        payload = json.dumps(current)
        script = f"""
        let clock = Date.parse({json.dumps(stamp(NOW))});
        Date.now = () => clock;
        const nodes = {{
          "uptime-endpoints": {{innerHTML: ""}},
          "uptime-updated": {{innerHTML: ""}}
        }};
        let interval, cancelled = 0, options;
        global.setTimeout = () => 1;
        global.clearTimeout = () => {{}};
        global.setInterval = callback => {{ interval = callback; }};
        global.window = {{matchMedia: () => ({{matches:false}})}};
        global.document = {{
          getElementById: id => nodes[id] || null,
          addEventListener: (_, callback) => callback(),
          querySelector: () => null
        }};
        global.fetch = async (_, settings) => {{
          options = settings;
          if ({str(bad_fetch).lower()}) throw Error("unavailable");
          const body = new TextEncoder().encode({str(oversized).lower()} ? "x".repeat(16385) : JSON.stringify({payload}));
          let read = false;
          return {{ok:true, body:{{getReader:() => ({{
            read:async () => read ? {{done:true}} : (read=true, {{done:false,value:body}}),
            cancel:async () => {{cancelled++;}}
          }})}}}};
        }};
        require({source});
        (async () => {{
          await new Promise(resolve => setImmediate(resolve));
          if ({advance}) {{
            clock += {advance} * 60000;
            await interval();
          }}
          process.stdout.write(JSON.stringify({{
            cards:nodes["uptime-endpoints"].innerHTML,
            freshness:nodes["uptime-updated"].innerHTML,
            options:{{cache:options.cache,credentials:options.credentials,redirect:options.redirect}},
            cancelled
          }}));
        }})().catch(error => {{ console.error(error); process.exitCode=1; }});
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=20)
        return json.loads(result.stdout)

    def test_fresh_expected_health_is_green_and_shows_latency(self):
        rendered = self.render(report())
        self.assertEqual(rendered["cards"].count('class="badge ok"'), 4)
        self.assertIn("10 ms", rendered["cards"])
        self.assertIn("observations current", rendered["freshness"])
        self.assertEqual(rendered["options"], {"cache": "no-store", "credentials": "omit", "redirect": "error"})

    def test_stale_missing_future_failed_and_oversized_reports_are_not_green(self):
        for current, arguments in (
            (None, {}), (report(), {"bad_fetch": True}),
            (report(), {"advance": 46}), (report(), {"oversized": True}),
            ({**report(), "checkedAt": stamp(NOW + timedelta(seconds=1))}, {}),
        ):
            with self.subTest(arguments=arguments):
                rendered = self.render(current, **arguments)
                self.assertNotIn('class="badge ok"', rendered["cards"])
                self.assertEqual(rendered["cards"].count('class="badge unknown"'), 4)
                if arguments.get("oversized"):
                    self.assertEqual(rendered["cancelled"], 1)
        self.assertEqual(self.render(report(), advance=45)["cards"].count('class="badge ok"'), 4)

    def test_down_unknown_and_invalid_positive_metadata_never_render_healthy(self):
        current = report("down")
        current["endpoints"][0]["state"] = "unknown"
        current["endpoints"][1]["state"] = "up"
        current["endpoints"][2]["observedAt"] = stamp(NOW + timedelta(seconds=1))
        rendered = self.render(current)
        self.assertEqual(rendered["cards"].count('class="badge bad"'), 1)
        self.assertNotIn('class="badge ok"', rendered["cards"])


if __name__ == "__main__":
    unittest.main()
