"""Pin the OIDC Foundry asset reconciliation workflow's release contract."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "foundry-assets.yml"


class FoundryAssetsWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.document = yaml.safe_load(cls.raw)
        cls.triggers = cls.document.get("on", cls.document.get(True, {}))
        cls.job = cls.document["jobs"]["reconcile"]

    def test_runs_on_foundry_changes_and_manual_dispatch(self) -> None:
        self.assertEqual(self.triggers["push"]["branches"], ["main"])
        self.assertIn("foundry/**", self.triggers["push"]["paths"])
        self.assertIn("workflow_dispatch", self.triggers)

    def test_uses_oidc_and_an_explicit_repository_project_endpoint(self) -> None:
        self.assertEqual(
            self.document["permissions"], {"id-token": "write", "contents": "read"}
        )
        self.assertEqual(
            self.job["env"]["AZURE_FOUNDRY_PROJECT_ENDPOINT"],
            "${{ vars.AZURE_FOUNDRY_PROJECT_ENDPOINT }}",
        )
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

    def test_skills_are_ensured_before_toolbox_and_failures_are_not_suppressed(self) -> None:
        steps = self.job["steps"]
        skill_index = next(
            index for index, step in enumerate(steps) if step.get("name") == "Ensure Foundry skills"
        )
        toolbox_index = next(
            index for index, step in enumerate(steps) if step.get("name") == "Ensure Foundry toolbox"
        )
        self.assertLess(skill_index, toolbox_index)
        for index, script in (
            (skill_index, "scripts/provision-foundry-skills.py"),
            (toolbox_index, "scripts/provision-foundry-toolbox.py"),
        ):
            step = steps[index]
            self.assertIn("set -euo pipefail", step["run"])
            self.assertIn(script, step["run"])
            self.assertIn("--create", step["run"])
            self.assertIn("--project-endpoint", step["run"])
            self.assertNotIn("continue-on-error", step)


if __name__ == "__main__":
    unittest.main()
