"""Safety contracts for the one-time Lean Azure retained-resource cleanup."""
from __future__ import annotations

import shutil
import subprocess
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
    def run_script(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(SCRIPT), *BASE_ARGS, *extra],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_is_a_real_dry_run_without_azure_cli(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run complete. Nothing deleted.", result.stdout)
        self.assertIn("swagger-petstore", result.stdout)

    def test_execute_refuses_without_separate_acknowledgement(self) -> None:
        result = self.run_script("-Execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-AcknowledgeRetainedResourceDeletion", result.stderr)


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
        self.assertIn("'--include-inherited', 'false'", TEXT)

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
