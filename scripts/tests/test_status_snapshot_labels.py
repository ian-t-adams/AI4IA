"""Status snapshot generation must fail closed and label portal resources."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "scripts" / "status-snapshot.ps1"
ENDPOINTS = REPO / "scripts" / "status-endpoints.ps1"
SERVICES = REPO / "site" / "data" / "services.js"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _load_assignment(path: Path, variable: str) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    prefix = f"window.{variable} = "
    payload = text.split(prefix, 1)[1].rsplit(";", 1)[0]
    return json.loads(payload)


class StatusSnapshotLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("pwsh")
        if not cls.pwsh:
            raise RuntimeError("pwsh is required to exercise status-snapshot.ps1")

    def test_private_destination_guard_runs_before_discovery_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            script = scripts / SNAPSHOT.name
            shutil.copyfile(SNAPSHOT, script)
            site = root / "site"
            site.mkdir()
            sentinel = site / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            for destination in (
                None, site, site / "data", site / "data" / ".." / "other",
                # Case variants are rejected on every OS (Windows/macOS are commonly case-insensitive).
                root / "SITE" / "data", root / "Site",
            ):
                with self.subTest(destination=destination):
                    argument = "" if destination is None else f"-OutDir {_ps_quote(str(destination))}"
                    wrapper = f"""
                    function az {{ throw 'DISCOVERY_MUST_NOT_RUN' }}
                    function azd {{ throw 'DISCOVERY_MUST_NOT_RUN' }}
                    & {_ps_quote(str(script))} {argument}
                    """
                    result = subprocess.run(
                        [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", wrapper],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Private snapshots", result.stderr)
                    self.assertNotIn("DISCOVERY_MUST_NOT_RUN", result.stdout + result.stderr)
                    self.assertEqual(list(site.iterdir()), [sentinel])
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_legacy_powershell_is_refused_before_any_work(self) -> None:
        first = SNAPSHOT.read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertEqual(first.strip(), "#Requires -Version 7.0")
        legacy = shutil.which("powershell")
        if not legacy:
            self.skipTest("Windows PowerShell 5.1 is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            script = scripts / SNAPSHOT.name
            shutil.copyfile(SNAPSHOT, script)
            (root / "site" / "data").mkdir(parents=True)
            wrapper = f"""
            function az {{ throw 'DISCOVERY_MUST_NOT_RUN' }}
            function azd {{ throw 'DISCOVERY_MUST_NOT_RUN' }}
            & {_ps_quote(str(script))} -OutDir {_ps_quote(str(root / 'SITE' / 'data'))}
            """
            result = subprocess.run(
                [legacy, "-NoProfile", "-NonInteractive", "-Command", wrapper],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("DISCOVERY_MUST_NOT_RUN", result.stdout + result.stderr)
            self.assertEqual(list((root / "site" / "data").iterdir()), [])

    def test_explicit_private_destination_retains_operator_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_snapshot("valid", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = (root / "inventory.js").read_text(encoding="utf-8-sig")
            self.assertIn("sub-test", inventory)
            self.assertIn("rg-test", inventory)
            self.assertIn("sttest", inventory)

    def _run_snapshot(
        self, mode: str, out_dir: Path, *, public: bool = False, cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        resource_id = (
            "/subscriptions/sub-test/resourceGroups/rg-test/providers/"
            "Microsoft.Storage/storageAccounts/sttest"
        )
        payloads = {
            "malformed": "{not-json",
            "missing-data": json.dumps({"count": 0}),
            "non-array-data": json.dumps({"data": {"id": "wrong-shape"}}),
            "empty": json.dumps({"data": []}),
            "invalid-row": json.dumps(
                {
                    "data": [
                        {
                            "id": "/subscriptions/sub-test/resourceGroups/rg-test/providers/x",
                            "name": "broken",
                            "location": "eastus",
                        }
                    ]
                }
            ),
            "valid": json.dumps(
                {
                    "data": [
                        {
                            "id": resource_id,
                            "name": "sttest",
                            "type": "microsoft.storage/storageaccounts",
                            "location": "eastus",
                            "prov": "Succeeded",
                        }
                    ]
                }
            ),
        }
        inventory_json = payloads.get(mode, payloads["valid"])
        if mode.startswith("api-"):
            inventory = json.loads(inventory_json)
            inventory["data"].append({
                "id": resource_id.replace("Microsoft.Storage/storageAccounts/sttest",
                                          "Microsoft.App/containerApps/arbitrary-name"),
                "name": "arbitrary-name", "type": "microsoft.app/containerapps",
                "location": "eastus", "prov": "Succeeded", "service": "api",
                "ingressFqdn": "api.example.test", "ingressExternal": "true",
            })
            inventory_json = json.dumps(inventory)
        health_json = json.dumps(
            {
                "data": (
                    [
                        {
                            "rid": resource_id.lower(),
                            "state": (
                                "Degraded" if mode == "health-degraded" else "Available"
                            ),
                        }
                    ]
                    if mode in {"health-available", "health-degraded"}
                    else []
                )
            }
        )
        fixture_dir = out_dir / "scripts"
        fixture_dir.mkdir()
        fixture_script = fixture_dir / SNAPSHOT.name
        shutil.copyfile(SNAPSHOT, fixture_script)
        for module in ("public_status.py", "http_contract.py"):
            shutil.copyfile(SNAPSHOT.parent / module, fixture_dir / module)
        # Keep the entry point and classification code intact; replace only HTTP.
        (fixture_dir / ENDPOINTS.name).write_text(
            ENDPOINTS.read_text(encoding="utf-8") + """
            function Invoke-ApiHealthRequest {
                param([string] $Url)
                $global:healthUrls += $Url
                $ready = $Url.EndsWith('/health/ready')
                $failed = $ready -and $mode -eq 'api-store-unavailable'
                $body = if ($failed) {
                    '{"status":"unavailable","stage":"session_store"}'
                } elseif ($ready) {
                    '{"status":"ok","stage":"session_store"}'
                } else {
                    '{"status":"ok"}'
                }
                return @{
                    httpStatus = $(if ($failed) { 503 } else { 200 })
                    body = $body; contentType = 'application/json'; bodyValid = $true
                }
            }
            """,
            encoding="utf-8",
        )
        wrapper = textwrap.dedent(
            f"""
            $mode = {_ps_quote(mode)}
            $inventoryJson = {_ps_quote(inventory_json)}
            $healthJson = {_ps_quote(health_json)}
            $providerState = if ($mode -eq 'provider-unregistered') {{
                'NotRegistered'
            }} else {{
                'Registered'
            }}
            $global:graphQueries = @()
            $global:healthUrls = @()
            function global:azd {{ $global:LASTEXITCODE = 1 }}
            function global:az {{
                $joined = $args -join ' '
                if ($joined -like 'account set*') {{
                    $global:LASTEXITCODE = 0
                    return
                }}
                if ($joined -like 'extension list*') {{
                    Write-Output 'resource-graph'
                    $global:LASTEXITCODE = 0
                    return
                }}
                if ($joined -like 'provider show*') {{
                    Write-Output $providerState
                    $global:LASTEXITCODE = 0
                    return
                }}
                if ($joined -like 'graph query*') {{
                    $global:graphQueries += $joined
                    if ($joined -like '*healthresources*') {{
                        Write-Output $healthJson
                        $global:LASTEXITCODE = if ($mode -eq 'health-query-failure') {{ 19 }} else {{ 0 }}
                        return
                    }}
                    if ($mode -eq 'nonzero') {{
                        Write-Output $inventoryJson
                        $global:LASTEXITCODE = 17
                        return
                    }}
                    Write-Output $inventoryJson
                    $global:LASTEXITCODE = 0
                    return
                }}
                throw "Unexpected az invocation: $joined"
            }}
            $initialLocation = (Get-Location).Path
            & {_ps_quote(str(fixture_script))} `
                -Public:${str(public).lower()} `
                -Subscription 'sub-test' `
                -ResourceGroup 'rg-test' `
                -OutDir {_ps_quote(str(out_dir))}
            if ((Get-Location).Path -cne $initialLocation) {{
                throw 'Snapshot changed the caller location.'
            }}
            ConvertTo-Json -InputObject @($global:healthUrls) |
                Set-Content {_ps_quote(str(out_dir / "probes.json"))}
            if ($mode -eq 'valid') {{
                if ($global:graphQueries.Count -ne 2) {{
                    throw "Expected inventory and health queries, got $($global:graphQueries.Count)."
                }}
                $unscoped = @($global:graphQueries | Where-Object {{
                    $_ -notmatch '(?:^| )--subscriptions sub-test(?: |$)'
                }})
                if ($unscoped.Count -gt 0) {{
                    throw "Resource Graph query was not scoped to sub-test: $($unscoped -join '; ')"
                }}
                $healthQuery = @($global:graphQueries | Where-Object {{ $_ -like '*healthresources*' }})
                if ($healthQuery.Count -ne 1 -or $healthQuery[0] -notlike '*/resourcegroups/rg-test/*') {{
                    throw "Resource Health query was not filtered to rg-test."
                }}
            }}
            if ($mode -eq 'provider-unregistered' -and $global:graphQueries.Count -ne 1) {{
                throw "Resource Health should not be queried when its provider is not registered."
            }}
            """
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", wrapper],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )

    def test_public_projection_uses_script_repository_from_another_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as caller:
            root = Path(temporary)
            result = self._run_snapshot("api-healthy", root, public=True, cwd=Path(caller))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = (root / "inventory.js").read_text(encoding="utf-8-sig")
            self.assertIn("Storage Account", inventory)
            self.assertNotIn("sub-test", inventory)

    def test_public_snapshot_removes_identifiers_from_outputs_and_logs(self) -> None:
        for mode in ("api-healthy", "api-store-unavailable", "health-available",
                     "health-query-failure", "malformed", "nonzero", "empty"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = self._run_snapshot(mode, root, public=True)
                exported = result.stdout + result.stderr
                if mode not in {"malformed", "nonzero", "empty"}:
                    self.assertEqual(result.returncode, 0, exported)
                    exported += (root / "inventory.js").read_text(encoding="utf-8-sig")
                    exported += (root / "status.js").read_text(encoding="utf-8-sig")
                    self.assertIn("Storage Account", exported)
                else:
                    self.assertNotEqual(result.returncode, 0)
                for private in ("sub-test", "rg-test", "sttest", "eastus",
                                "arbitrary-name", "https://api.example.test"):
                    self.assertNotIn(private, exported)

    def test_required_inventory_failures_do_not_touch_either_output(self) -> None:
        for mode in (
            "nonzero",
            "malformed",
            "missing-data",
            "non-array-data",
            "empty",
            "invalid-row",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                out_dir = Path(tmp)
                inventory = out_dir / "inventory.js"
                status = out_dir / "status.js"
                inventory.write_text("inventory sentinel", encoding="utf-8")
                status.write_text("status sentinel", encoding="utf-8")

                result = self._run_snapshot(mode, out_dir)

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(inventory.read_text(encoding="utf-8"), "inventory sentinel")
                self.assertEqual(status.read_text(encoding="utf-8"), "status sentinel")

    def test_valid_nonempty_inventory_scopes_both_queries_and_writes_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("valid", out_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = (out_dir / "inventory.js").read_text(encoding="utf-8-sig")
            status = (out_dir / "status.js").read_text(encoding="utf-8-sig")
            self.assertIn("window.AI4IA_INVENTORY", inventory)
            self.assertIn('"name": "sttest"', inventory)
            self.assertIn("window.AI4IA_STATUS", status)
            self.assertIn('"total": 1', status)
            self.assertNotIn('"name": "sttest"', status)

    def test_available_health_signal_is_distinct_from_missing_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("health-available", out_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = _load_assignment(out_dir / "inventory.js", "AI4IA_INVENTORY")
            status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
            resource = inventory["resources"][0]
            self.assertEqual(resource["availability"], "Available")
            self.assertTrue(resource["healthReported"])
            self.assertEqual(resource["state"], "healthy")
            self.assertEqual(status["summary"]["healthy"], 1)
            self.assertEqual(status["summary"]["provisioned"], 0)
            self.assertEqual(status["healthSource"]["status"], "available")
            self.assertEqual(status["healthSource"]["records"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("valid", out_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = _load_assignment(out_dir / "inventory.js", "AI4IA_INVENTORY")
            status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
            resource = inventory["resources"][0]
            self.assertEqual(resource["availability"], "Unknown")
            self.assertFalse(resource["healthReported"])
            self.assertEqual(resource["state"], "provisioned")
            self.assertEqual(status["summary"]["healthy"], 0)
            self.assertEqual(status["summary"]["provisioned"], 1)
            self.assertEqual(status["healthSource"]["status"], "available")
            self.assertEqual(status["healthSource"]["records"], 0)

    def test_unregistered_provider_is_reported_as_a_source_outage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("provider-unregistered", out_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = _load_assignment(out_dir / "inventory.js", "AI4IA_INVENTORY")
            status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
            self.assertEqual(status["healthSource"]["status"], "unavailable")
            self.assertEqual(status["healthSource"]["providerState"], "NotRegistered")
            self.assertIn("not registered", status["healthSource"]["note"].lower())
            self.assertFalse(inventory["resources"][0]["healthReported"])

    def test_health_query_failure_is_not_published_as_no_resource_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("health-query-failure", out_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
            self.assertEqual(status["healthSource"]["status"], "unavailable")
            self.assertIn("could not be queried", status["healthSource"]["note"].lower())

    def test_resource_health_degraded_state_is_counted_as_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("health-degraded", out_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inventory = _load_assignment(out_dir / "inventory.js", "AI4IA_INVENTORY")
            status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
            self.assertEqual(inventory["resources"][0]["state"], "degraded")
            self.assertEqual(status["summary"]["degraded"], 1)
            self.assertEqual(status["summary"]["provisioned"], 0)

    def test_tagged_api_health_is_published_without_inventory_metadata_or_response_content(self) -> None:
        for mode, passing, outcome in (
            ("api-healthy", 2, "healthy"),
            ("api-store-unavailable", 1, "persistence_unavailable"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                out_dir = Path(tmp)
                result = self._run_snapshot(mode, out_dir)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
                inventory = _load_assignment(out_dir / "inventory.js", "AI4IA_INVENTORY")
                self.assertEqual(status["summary"]["endpointsTot"], 4)
                self.assertEqual(status["summary"]["endpointsUp"], passing)
                self.assertEqual(status["endpoints"][2]["kind"], "liveness")
                self.assertTrue(status["endpoints"][2]["ok"])
                self.assertEqual(status["endpoints"][3]["kind"], "readiness")
                self.assertEqual(status["endpoints"][3]["outcome"], outcome)
                self.assertIsNotNone(status["endpoints"][3]["observedAt"])
                self.assertEqual(
                    json.loads((out_dir / "probes.json").read_text(encoding="utf-8-sig")),
                    ["https://api.example.test/health/live", "https://api.example.test/health/ready"],
                )
                self.assertNotIn("ingressFqdn", json.dumps(inventory))
                self.assertNotIn("ingressExternal", json.dumps(inventory))
                self.assertNotIn("body", json.dumps(status))

    def test_unresolved_api_is_unobserved_not_omitted_or_reported_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = self._run_snapshot("valid", out_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = _load_assignment(out_dir / "status.js", "AI4IA_STATUS")
            self.assertEqual(status["summary"]["endpointsTot"], 4)
            self.assertEqual(status["summary"]["endpointsUp"], 0)
            for endpoint in status["endpoints"][2:]:
                self.assertFalse(endpoint["ok"])
                self.assertEqual(endpoint["outcome"], "target_unresolved")
                self.assertIsNone(endpoint["observedAt"])
            self.assertEqual(
                json.loads((out_dir / "probes.json").read_text(encoding="utf-8-sig")), [],
            )


class StatusSnapshotCatalogTests(unittest.TestCase):
    def test_durable_task_is_named_and_grouped_consistently(self) -> None:
        snapshot = SNAPSHOT.read_text(encoding="utf-8")
        services = SERVICES.read_text(encoding="utf-8")

        self.assertIn(
            "'microsoft.durabletask/schedulers'",
            snapshot,
            "a deployed scheduler must not render as an unknown 'Other' resource",
        )
        self.assertIn("label = 'Durable Task Scheduler'", snapshot)
        self.assertIn(
            'azureType: "Microsoft.DurableTask/schedulers"',
            services,
            "the Services page claims to catalog every deployed Azure service",
        )
        self.assertIn('name: "Durable Task Scheduler"', services)


if __name__ == "__main__":
    unittest.main()
