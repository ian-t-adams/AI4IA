"""Pin the OIDC Foundry asset reconciliation workflow's release contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "foundry-assets.yml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
REMOVED_SKILL_REFERENCES = (
    "citation-" + "discipline",
    "provision-foundry-" + "skills.py",
    "toolbox/" + "skills scripts",
)


class FoundryAssetsWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.document = yaml.safe_load(cls.raw)
        cls.triggers = cls.document.get("on", cls.document.get(True, {}))
        cls.gate = cls.document["jobs"]["gate"]
        cls.job = cls.document["jobs"]["reconcile"]

    def test_runs_after_successful_main_deploy_and_on_manual_dispatch(self) -> None:
        self.assertNotIn("push", self.triggers)
        self.assertEqual(self.triggers["workflow_run"]["workflows"], ["deploy"])
        self.assertEqual(self.triggers["workflow_run"]["types"], ["completed"])
        self.assertIn("workflow_dispatch", self.triggers)
        manual_input = self.triggers["workflow_dispatch"]["inputs"]["project_endpoint"]
        self.assertTrue(manual_input["required"])
        self.assertEqual("string", manual_input["type"])
        self.assertIn("azd env get-value", manual_input["description"])
        condition = self.gate["if"]
        self.assertIn("github.event.workflow_run.conclusion == 'success'", condition)
        self.assertIn("github.event.workflow_run.event == 'push'", condition)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", condition)
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertEqual(["gate"], [self.job["needs"]])
        self.assertEqual(
            "${{ needs.gate.outputs.should_reconcile == 'true' }}",
            self.job["if"],
        )

    def test_unprivileged_gate_requires_exact_run_artifact(self) -> None:
        self.assertEqual({}, self.document["permissions"])
        self.assertNotIn("concurrency", self.document)
        self.assertEqual({"actions": "read"}, self.gate["permissions"])
        self.assertNotIn("environment", self.gate)
        self.assertNotIn("id-token", self.gate["permissions"])
        inspect = next(
            step
            for step in self.gate["steps"]
            if step.get("name") == "Inspect triggering deploy job"
        )
        self.assertEqual("${{ github.token }}", inspect["env"]["GH_TOKEN"])
        self.assertEqual(
            "${{ github.event.workflow_run.id }}",
            inspect["env"]["TRIGGER_RUN_ID"],
        )
        self.assertIn("/actions/runs/${TRIGGER_RUN_ID}/jobs", inspect["run"])
        self.assertIn('select(.name == "deploy")', inspect["run"])
        self.assertIn('success) echo "deploy_ran=true"', inspect["run"])
        self.assertIn('skipped) echo "deploy_ran=false"', inspect["run"])
        probe = next(
            step
            for step in self.gate["steps"]
            if step.get("name") == "Download triggering deploy artifact"
        )
        self.assertNotIn("continue-on-error", probe)
        self.assertEqual(
            "${{ steps.deploy.outputs.deploy_ran == 'true' }}",
            probe["if"],
        )
        self.assertEqual(
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            probe["uses"],
        )
        self.assertEqual(
            "${{ github.event.workflow_run.id }}",
            probe["with"]["run-id"],
        )
        decide = next(
            step
            for step in self.gate["steps"]
            if step.get("name") == "Decide whether reconciliation is applicable"
        )
        self.assertEqual(
            "${{ steps.deploy.outputs.deploy_ran }}",
            decide["env"]["DEPLOY_RAN"],
        )
        self.assertEqual(
            "${{ inputs.project_endpoint }}",
            decide["env"]["MANUAL_PROJECT_ENDPOINT"],
        )
        self.assertIn('DEPLOY_RAN" = "false', decide["run"])
        self.assertIn(".foundry-assets-gate/enabled.txt", decide["run"])
        self.assertIn('feature_state" = "false', decide["run"])
        self.assertIn('feature_state" != "true', decide["run"])
        self.assertIn(".foundry-assets-gate/project-endpoint.txt", decide["run"])
        self.assertIn("*$'\\n'*", decide["run"])
        self.assertIn("invalid shape", decide["run"])
        self.assertIn("should_reconcile=false", decide["run"])
        self.assertIn("project_endpoint=%s", decide["run"])
        self.assertEqual(
            "${{ steps.decide.outputs.project_endpoint }}",
            self.gate["outputs"]["project_endpoint"],
        )

    def test_deploy_runs_for_foundry_assets_before_reconciliation(self) -> None:
        document = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
        triggers = document.get("on", document.get(True, {}))
        paths = triggers["push"]["paths"]
        self.assertIn("foundry/**", paths)
        self.assertIn("scripts/provision-foundry-toolbox.py", paths)
        self.assertIn(".github/workflows/foundry-assets.yml", paths)
        self.assertNotIn("name", document["jobs"]["deploy"])
        steps = document["jobs"]["deploy"]["steps"]
        provision_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Provision infrastructure"
        )
        prepare_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Prepare Foundry asset handoff"
        )
        upload_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Retain Foundry asset handoff"
        )
        self.assertLess(provision_index, prepare_index)
        self.assertLess(prepare_index, upload_index)
        prepare = steps[prepare_index]
        self.assertEqual(
            "${{ steps.provision.outcome == 'success' }}",
            prepare["if"],
        )
        self.assertIn(
            "azd env get-value AZURE_FOUNDRY_PROJECT_ENDPOINT",
            prepare["run"],
        )
        self.assertIn('[ -z "$endpoint" ]', prepare["run"])
        self.assertIn("foundry-assets-context/enabled.txt", prepare["run"])
        self.assertIn("printf 'false\\n'", prepare["run"])
        self.assertIn("printf 'true\\n'", prepare["run"])
        self.assertIn("invalid Foundry project endpoint", prepare["run"])
        upload = steps[upload_index]
        self.assertEqual(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            upload["uses"],
        )
        self.assertEqual("foundry-assets-context", upload["with"]["name"])
        self.assertEqual(
            "${{ runner.temp }}/foundry-assets-context",
            upload["with"]["path"],
        )
        self.assertEqual(30, upload["with"]["retention-days"])
        self.assertEqual("error", upload["with"]["if-no-files-found"])

    def test_workflow_run_checks_out_the_exact_deployed_sha(self) -> None:
        checkout = next(
            step for step in self.job["steps"] if step.get("name") == "Checkout"
        )
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || github.sha }}",
        )

    def test_uses_oidc_and_exact_deploy_artifact_or_manual_endpoint(self) -> None:
        self.assertEqual(
            self.job["permissions"],
            {"id-token": "write", "contents": "read"},
        )
        self.assertEqual(self.job["environment"], "production")
        self.assertEqual(
            "${{ needs.gate.outputs.project_endpoint }}",
            self.job["env"]["AZURE_FOUNDRY_PROJECT_ENDPOINT"],
        )
        self.assertEqual("foundry-assets-production", self.job["concurrency"]["group"])
        self.assertFalse(self.job["concurrency"]["cancel-in-progress"])
        self.assertEqual(
            [],
            [
                step["name"]
                for step in self.job["steps"]
                if "download-artifact@" in step.get("uses", "")
            ],
        )
        self.assertNotIn("vars.AZURE_FOUNDRY_PROJECT_ENDPOINT", self.raw)
        self.assertNotIn("vars.AI4IA_PRODUCTION_FOUNDRY_PROJECT_ENDPOINT", self.raw)
        self.assertNotIn(".services.ai.azure.com/api/projects/", self.raw)
        login = next(
            step for step in self.job["steps"] if step.get("name") == "Log in to Azure (OIDC)"
        )
        self.assertEqual(
            login["uses"],
            "azure/login@f5d393ae46f8fde4be8b75f32e3fc50e654ad0ca",
        )
        self.assertEqual(login["with"]["client-id"], "${{ vars.AZURE_CLIENT_ID }}")
        self.assertEqual(login["with"]["tenant-id"], "${{ vars.AZURE_TENANT_ID }}")
        self.assertEqual(
            login["with"]["subscription-id"], "${{ vars.AZURE_SUBSCRIPTION_ID }}"
        )

    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    @unittest.skipIf(os.name == "nt", "requires POSIX environment forwarding")
    def test_gate_endpoint_controls_rejections_and_disabled_deploy(self) -> None:
        decide = next(
            step
            for step in self.gate["steps"]
            if step.get("name") == "Decide whether reconciliation is applicable"
        )

        def run(
            endpoint: str,
            *,
            event_name: str,
            deploy_ran: str,
            feature_enabled: bool = True,
        ) -> tuple[subprocess.CompletedProcess[str], str]:
            with tempfile.TemporaryDirectory() as temp:
                temp_path = Path(temp)
                output_file = temp_path / "github-output"
                if event_name == "workflow_run":
                    artifact = temp_path / ".foundry-assets-gate"
                    artifact.mkdir()
                    (artifact / "enabled.txt").write_text(
                        f"{str(feature_enabled).lower()}\n",
                        encoding="utf-8",
                    )
                    if feature_enabled:
                        (artifact / "project-endpoint.txt").write_text(
                            f"{endpoint}\n",
                            encoding="utf-8",
                        )
                completed = subprocess.run(
                    ["bash", "-c", decide["run"]],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=temp_path,
                    env={
                        **os.environ,
                        "EVENT_NAME": event_name,
                        "DEPLOY_RAN": deploy_ran,
                        "MANUAL_PROJECT_ENDPOINT": (
                            endpoint if event_name == "workflow_dispatch" else ""
                        ),
                        "GITHUB_OUTPUT": output_file.as_posix(),
                    },
                )
                exported = (
                    output_file.read_text(encoding="utf-8")
                    if output_file.exists()
                    else ""
                )
                return completed, exported

        valid = (
            "https://mf-ai4ia-prod-eastus2-abc123.services.ai.azure.com"
            "/api/projects/proj-default-ai4ia-prod-eastus2"
        )
        for event_name, deploy_ran in (
            ("workflow_dispatch", "manual"),
            ("workflow_run", "true"),
        ):
            with self.subTest(event_name=event_name):
                accepted, exported = run(
                    valid,
                    event_name=event_name,
                    deploy_ran=deploy_ran,
                )
                self.assertEqual(0, accepted.returncode, accepted.stderr)
                self.assertEqual(
                    f"should_reconcile=true\nproject_endpoint={valid}\n",
                    exported,
                )

        multiline, exported = run(
            f"{valid}\nINJECTED=value",
            event_name="workflow_dispatch",
            deploy_ran="manual",
        )
        self.assertNotEqual(0, multiline.returncode)
        self.assertEqual("", exported)

        foreign, exported = run(
            "https://evil.example/api/projects/not-foundry",
            event_name="workflow_dispatch",
            deploy_ran="manual",
        )
        self.assertNotEqual(0, foreign.returncode)
        self.assertEqual("", exported)

        disabled, exported = run(
            valid,
            event_name="workflow_run",
            deploy_ran="false",
        )
        self.assertEqual(0, disabled.returncode, disabled.stderr)
        self.assertEqual("should_reconcile=false\n", exported)

        feature_off, exported = run(
            valid,
            event_name="workflow_run",
            deploy_ran="true",
            feature_enabled=False,
        )
        self.assertEqual(0, feature_off.returncode, feature_off.stderr)
        self.assertEqual("should_reconcile=false\n", exported)

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

    def test_deleted_legacy_skill_has_no_stale_repository_references(self) -> None:
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

    def test_active_skill_is_owned_by_the_toolbox_provisioner(self) -> None:
        manifest = (ROOT / "foundry" / "toolbox.manifest.json").read_text(
            encoding="utf-8"
        )
        provisioner = (
            ROOT / "scripts" / "provision-foundry-toolbox.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"name": "evidence-review"', manifest)
        self.assertTrue(
            (ROOT / "foundry" / "skills" / "evidence-review" / "SKILL.md").is_file()
        )
        self.assertIn("manifest_skill_sources", provisioner)
        self.assertIn("ensure_manifest_skills", provisioner)


if __name__ == "__main__":
    unittest.main()
