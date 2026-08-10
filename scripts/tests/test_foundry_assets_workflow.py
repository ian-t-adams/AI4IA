"""Pin the OIDC Foundry asset reconciliation workflow's release contract."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "foundry-assets.yml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
REMOVED_SKILL_REFERENCES = (
    "citation-" + "discipline",
    "provision-foundry-" + "skills.py",
    "foundry/" + "skills/",
    "SKILL.md parse/" + "validate",
    "toolbox/" + "skills scripts",
)


class FoundryAssetsWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.document = yaml.safe_load(cls.raw)
        cls.triggers = cls.document.get("on", cls.document.get(True, {}))
        cls.job = cls.document["jobs"]["reconcile"]

    def test_runs_after_successful_main_deploy_and_on_manual_dispatch(self) -> None:
        self.assertNotIn("push", self.triggers)
        self.assertEqual(self.triggers["workflow_run"]["workflows"], ["deploy"])
        self.assertEqual(self.triggers["workflow_run"]["types"], ["completed"])
        self.assertIn("workflow_dispatch", self.triggers)
        condition = self.job["if"]
        self.assertIn("github.event.workflow_run.conclusion == 'success'", condition)
        self.assertIn("github.event.workflow_run.event == 'push'", condition)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", condition)
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)

    def test_deploy_runs_for_foundry_assets_before_reconciliation(self) -> None:
        document = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
        triggers = document.get("on", document.get(True, {}))
        paths = triggers["push"]["paths"]
        self.assertIn("foundry/**", paths)
        self.assertIn("scripts/provision-foundry-toolbox.py", paths)
        self.assertIn(".github/workflows/foundry-assets.yml", paths)

    def test_workflow_run_checks_out_the_exact_deployed_sha(self) -> None:
        checkout = next(
            step for step in self.job["steps"] if step.get("name") == "Checkout"
        )
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || github.sha }}",
        )

    def test_uses_oidc_and_the_production_environment_project_endpoint(self) -> None:
        self.assertEqual(
            self.document["permissions"],
            {"id-token": "write", "contents": "read"},
        )
        self.assertNotIn("env", self.job)
        self.assertEqual(self.job["environment"], "production")
        self.assertIn(
            "Set AZURE_FOUNDRY_PROJECT_ENDPOINT in the production environment variables",
            self.raw,
        )
        self.assertNotIn("repository or production-environment variable", self.raw)
        resolve = next(
            step
            for step in self.job["steps"]
            if step.get("name") == "Resolve production project endpoint"
        )
        self.assertEqual(
            resolve["env"]["PRODUCTION_PROJECT_ENDPOINT"],
            "${{ vars.AZURE_FOUNDRY_PROJECT_ENDPOINT }}",
        )
        self.assertIn("PRODUCTION_PROJECT_ENDPOINT", resolve["run"])
        self.assertIn("$GITHUB_ENV", resolve["run"])
        self.assertNotIn("gh variable get", resolve["run"])
        self.assertNotIn(".services.ai.azure.com/api/projects/", self.raw)
        login = next(
            step for step in self.job["steps"] if step.get("name") == "Log in to Azure (OIDC)"
        )
        self.assertEqual(
            login["uses"],
            "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43",
        )
        self.assertEqual(login["with"]["client-id"], "${{ vars.AZURE_CLIENT_ID }}")
        self.assertEqual(login["with"]["tenant-id"], "${{ vars.AZURE_TENANT_ID }}")
        self.assertEqual(
            login["with"]["subscription-id"], "${{ vars.AZURE_SUBSCRIPTION_ID }}"
        )

    def test_access_preflight_precedes_toolbox_and_failures_are_not_suppressed(self) -> None:
        steps = self.job["steps"]
        access_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Verify Foundry toolbox data-plane access"
        )
        toolbox_index = next(
            index for index, step in enumerate(steps) if step.get("name") == "Ensure Foundry toolbox"
        )
        resolve_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Resolve production project endpoint"
        )
        self.assertLess(resolve_index, access_index)
        self.assertLess(access_index, toolbox_index)
        for index in (access_index, toolbox_index):
            step = steps[index]
            self.assertIn("set -euo pipefail", step["run"])
            self.assertIn("scripts/provision-foundry-toolbox.py", step["run"])
            self.assertIn("--project-endpoint", step["run"])
            self.assertNotIn("continue-on-error", step)
        self.assertIn("--check-access", steps[access_index]["run"])
        self.assertIn("--create", steps[toolbox_index]["run"])
        self.assertNotIn("--manifest", steps[toolbox_index]["run"])

    def test_deleted_skill_has_no_stale_repository_references(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode().split("\0")
        stale: list[str] = []
        for relative_path in tracked:
            if not relative_path:
                continue
            try:
                text = (ROOT / relative_path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for reference in REMOVED_SKILL_REFERENCES:
                if reference.lower() in text.lower():
                    stale.append(f"{relative_path}: {reference}")
        self.assertEqual([], stale)


if __name__ == "__main__":
    unittest.main()
