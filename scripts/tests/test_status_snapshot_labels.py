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

    def _run_snapshot(self, mode: str, out_dir: Path) -> subprocess.CompletedProcess[str]:
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
            & {_ps_quote(str(SNAPSHOT))} `
                -Subscription 'sub-test' `
                -ResourceGroup 'rg-test' `
                -OutDir {_ps_quote(str(out_dir))}
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
        )

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
