"""Safety contracts for the one-time Lean Azure retained-resource cleanup."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "cleanup-lean-azure-retained.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
RESOURCE_GROUP = "rg-ai4ia-production"
BASE_ARGS = [
    "-EventHubsNamespaceResourceId",
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.EventHub/namespaces/eh-ai4ia-production",
    "-MonitorWorkspaceResourceId",
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Monitor/accounts/prom-ai4ia-production",
    "-ApiCenterSampleApiResourceId",
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.ApiCenter/services/apic-ai4ia-production/workspaces/default/apis/swagger-petstore",
]


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required")
class CleanupExecutionTests(unittest.TestCase):
    def run_script(
        self, *extra: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(SCRIPT), *BASE_ARGS, *extra],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    @staticmethod
    def write_az_stub(directory: Path) -> Path:
        log = directory / "az-calls.jsonl"
        (directory / "az.ps1").write_text(
            """$switchOnly = @('--include-inherited', '--only-show-errors')
if ($args.Count -gt 1) {
    for ($index = 0; $index -lt $args.Count - 1; $index++) {
        if (($switchOnly -contains $args[$index]) -and -not $args[$index + 1].StartsWith('-')) {
            [Console]::Error.WriteLine("unrecognized arguments: $($args[$index + 1])")
            exit 2
        }
    }
}
Add-Content -LiteralPath $env:AZ_STUB_LOG -Value (ConvertTo-Json -InputObject ([object[]]$args) -Compress)
if ($args.Count -ge 3 -and $args[0] -eq 'role' -and $args[1] -eq 'assignment' -and $args[2] -eq 'list') {
    Write-Output '/subscriptions/test/providers/Microsoft.Authorization/roleAssignments/role-one'
    Write-Output '/subscriptions/test/providers/Microsoft.Authorization/roleAssignments/role-two'
}
$global:LASTEXITCODE = 0
""",
            encoding="utf-8",
        )
        return log

    def test_default_is_a_real_dry_run_without_azure_cli(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run complete. Nothing deleted.", result.stdout)
        self.assertIn("swagger-petstore", result.stdout)

    def test_execute_refuses_without_separate_acknowledgement(self) -> None:
        result = self.run_script("-Execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-AcknowledgeRetainedResourceDeletion", result.stderr)

    def test_execute_uses_valid_switches_and_deletes_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            stub_directory = Path(temporary_directory)
            log = self.write_az_stub(stub_directory)
            env = os.environ.copy()
            env["AZ_STUB_LOG"] = str(log)
            env["PATH"] = str(stub_directory) + os.pathsep + env["PATH"]

            result = self.run_script(
                "-Execute", "-AcknowledgeRetainedResourceDeletion", env=env
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([call[:2] for call in calls[:3]], [["resource", "show"]] * 3)
            self.assertEqual(calls[3][:3], ["role", "assignment", "list"])
            self.assertNotIn("--include-inherited", calls[3])
            self.assertEqual(
                [call[:3] for call in calls[4:6]],
                [["role", "assignment", "delete"]] * 2,
            )
            self.assertEqual(
                [call[:2] for call in calls[6:]],
                [["resource", "delete"]] * 3,
            )
            deleted_ids = [call[call.index("--ids") + 1] for call in calls[6:]]
            self.assertEqual(deleted_ids, [BASE_ARGS[5], BASE_ARGS[1], BASE_ARGS[3]])


class CleanupSourceContracts(unittest.TestCase):
    def test_targets_are_exact_resource_types_and_one_known_sample(self) -> None:
        for resource_path in (
            r"Microsoft\.EventHub/namespaces/[^/]+$",
            r"Microsoft\.Monitor/accounts/[^/]+$",
            r"Microsoft\.ApiCenter/services/[^/]+/workspaces/default/apis/swagger-petstore$",
        ):
            self.assertIn(resource_path, TEXT)
        self.assertNotIn("az group delete", TEXT)

    def test_direct_event_hubs_roles_are_removed_before_the_namespace(self) -> None:
        list_index = TEXT.index("'role', 'assignment', 'list'")
        role_delete_index = TEXT.index("'role', 'assignment', 'delete'")
        resource_delete_index = TEXT.index("'resource', 'delete'")
        self.assertLess(list_index, role_delete_index)
        self.assertLess(role_delete_index, resource_delete_index)
        self.assertNotIn("'--include-inherited'", TEXT)

    def test_cleanup_is_never_an_automatic_deployment_hook(self) -> None:
        for path in (
            ROOT / "azure.yaml",
            ROOT / ".github" / "workflows" / "deploy.yml",
            ROOT / "scripts" / "postprovision.ps1",
        ):
            self.assertNotIn(SCRIPT.name, path.read_text(encoding="utf-8"))

    def test_runbook_names_both_destructive_switches(self) -> None:
        runbook = (ROOT / "docs" / "runbooks" / "teardown.md").read_text(encoding="utf-8")
        self.assertIn("cleanup-lean-azure-retained.ps1", runbook)
        self.assertIn("-Execute -AcknowledgeRetainedResourceDeletion", runbook)


if __name__ == "__main__":
    unittest.main()
