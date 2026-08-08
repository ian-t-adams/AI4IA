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


class StatusSnapshotLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("pwsh")
        if not cls.pwsh:
            raise RuntimeError("pwsh is required to exercise status-snapshot.ps1")

    def _run_snapshot(self, mode: str, out_dir: Path) -> subprocess.CompletedProcess[str]:
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
                            "id": (
                                "/subscriptions/sub-test/resourceGroups/rg-test/providers/"
                                "Microsoft.Storage/storageAccounts/sttest"
                            ),
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
        wrapper = textwrap.dedent(
            f"""
            $mode = {_ps_quote(mode)}
            $inventoryJson = {_ps_quote(inventory_json)}
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
                if ($joined -like 'graph query*') {{
                    if ($joined -like '*healthresources*') {{
                        Write-Output '{{"data":[]}}'
                        $global:LASTEXITCODE = 0
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

    def test_valid_nonempty_inventory_reaches_both_output_writes(self) -> None:
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
