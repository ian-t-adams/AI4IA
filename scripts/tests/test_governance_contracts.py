"""Cross-file governance contracts that no single surface can enforce alone.

Each test here asserts on code, configuration, or a machine-readable manifest --
never on prose. A guard that pins an English sentence freezes the wording rather
than the behaviour, and is satisfied by a writer who copies the sentence into a
document that has since become false.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class ToolGovernanceTests(unittest.TestCase):
    def test_attachment_analysis_is_always_held_for_approval(self) -> None:
        """`analyze_attachment` must stay external-risk, so it is never auto-approved.

        Downgrading it to an injection-only risk would let an attachment be
        analyzed without a per-turn prompt, which is the whole control.
        """
        governance = read("app/api/src/ai4ia_api/agents/synthetic_governance.py")
        block = re.search(
            r"_ANALYZE_ATTACHMENT\s*=\s*ToolSpec\((.*?)\n\)",
            governance,
            re.DOTALL,
        )
        self.assertIsNotNone(block, "_ANALYZE_ATTACHMENT ToolSpec not found")
        assert block is not None
        self.assertIn("risk=ToolRisk.external", block.group(1))
        self.assertNotIn("injection_only_risk", block.group(1))


class FoundryManifestContractTests(unittest.TestCase):
    """The Foundry manifests are a machine contract, not documentation."""

    def setUp(self) -> None:
        self.toolbox = json.loads(read("foundry/toolbox.manifest.json"))
        self.routine = json.loads(read("foundry/routines/example.routine.json"))
        self.a2a = json.loads(read("foundry/a2a/example.a2a.json"))

    def test_every_manifest_pins_the_same_sdk_contract(self) -> None:
        for name, manifest in (
            ("toolbox", self.toolbox),
            ("routine", self.routine),
            ("a2a", self.a2a),
        ):
            with self.subTest(manifest=name):
                self.assertEqual("1.0", manifest["manifestVersion"])
                self.assertTrue(manifest["owner"])
                self.assertEqual("azure-ai-projects", manifest["sdkContract"]["package"])
                self.assertEqual("2.5.0", manifest["sdkContract"]["version"])

    def test_only_the_toolbox_is_executable(self) -> None:
        """Routine and A2A are design artifacts; nothing may reconcile them."""
        example_toolbox = json.loads(read("foundry/toolbox.manifest.example.json"))
        self.assertEqual("active", self.toolbox["lifecycle"])
        self.assertEqual("validated", self.toolbox["sdkContract"]["status"])
        self.assertEqual("reference", example_toolbox["lifecycle"])
        for name, manifest in (("routine", self.routine), ("a2a", self.a2a)):
            with self.subTest(manifest=name):
                self.assertEqual("design-preview", manifest["lifecycle"])
                self.assertEqual("not-executable", manifest["sdkContract"]["status"])

        a2a_script = read("scripts/provision-foundry-a2a.py")
        for emitter in ("to_az_commands", "build_agent_link", "--emit-az"):
            self.assertNotIn(
                emitter,
                a2a_script,
                f"the A2A provisioner regained {emitter!r}; it is validation-only",
            )
        self.assertIn("lifecycle='active'", read("scripts/provision-foundry-toolbox.py"))

    def test_the_routine_only_references_tools_the_toolbox_defines(self) -> None:
        canonical = {
            tool.get("name") or tool.get("serverLabel") for tool in self.toolbox["tools"]
        }
        referenced = {
            tool for step in self.routine["steps"] for tool in step.get("tools", [])
        }
        self.assertTrue(referenced, "the example routine references no tools at all")
        self.assertLessEqual(referenced, canonical)

    def test_design_artifacts_are_labelled_design_only(self) -> None:
        surfaces = "\n".join(
            read(path)
            for path in (
                "foundry/routines/routine.schema.json",
                "foundry/routines/example.routine.json",
                "foundry/a2a/a2a.schema.json",
                "foundry/a2a/example.a2a.json",
                "scripts/provision-foundry-routine.py",
                "scripts/provision-foundry-a2a.py",
            )
        )
        self.assertIn("DESIGN-ONLY", surfaces)


class WorkflowWiringTests(unittest.TestCase):
    def test_foundry_endpoint_travels_by_artifact_not_repository_variable(self) -> None:
        """A stored endpoint variable can silently target a stale environment.

        The deploy publishes the endpoint azd actually produced; the reconciler
        reads it back from that exact run, or takes an explicit manual input.
        """
        deploy = read(".github/workflows/deploy.yml")
        workflow = read(".github/workflows/foundry-assets.yml")

        self.assertIn("azd env get-value AZURE_FOUNDRY_PROJECT_ENDPOINT", deploy)
        self.assertIn("actions/upload-artifact@", deploy)
        self.assertIn("retention-days: 30", deploy)

        self.assertIn("actions/download-artifact@", workflow)
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", workflow)
        self.assertIn("MANUAL_PROJECT_ENDPOINT: ${{ inputs.project_endpoint }}", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn(
            "AZURE_FOUNDRY_PROJECT_ENDPOINT: ${{ needs.gate.outputs.project_endpoint }}",
            workflow,
        )
        for stored in (
            "vars.AZURE_FOUNDRY_PROJECT_ENDPOINT",
            "vars.AI4IA_PRODUCTION_FOUNDRY_PROJECT_ENDPOINT",
        ):
            self.assertNotIn(stored, workflow)

    def test_design_only_provisioners_stay_check_only_in_ci(self) -> None:
        infra_validate = read(".github/workflows/infra-validate.yml")
        self.assertIn("provision-foundry-routine.py --check", infra_validate)
        self.assertIn("provision-foundry-a2a.py --check", infra_validate)


class DeployableSurfaceTests(unittest.TestCase):
    def test_network_isolation_parameters_are_unreachable_from_a_deploy(self) -> None:
        """The private-network graph is incomplete scaffolding.

        Bicep still carries the parameters, but neither the parameter file nor
        the workflow may set them: a partially wired isolation mode would look
        enabled while leaving the control and data planes publicly reachable.
        """
        parameters = read("infra/main.parameters.json")
        deploy = read(".github/workflows/deploy.yml")
        for unreachable in ("vnetIsolationEnabled", "dataTierPrivate"):
            with self.subTest(parameter=unreachable):
                self.assertNotIn(f'"{unreachable}"', parameters)
                self.assertNotIn(unreachable, deploy)

    def test_apim_identifiers_are_template_outputs(self) -> None:
        """Key rotation derives APIM from outputs rather than a name search."""
        main_bicep = read("infra/main.bicep")
        self.assertIn("output AZURE_APIM_NAME string", main_bicep)
        self.assertIn("output AZURE_APIM_RESOURCE_ID string", main_bicep)

    def test_security_policy_names_the_only_direct_model_exception(self) -> None:
        """The gateway-first rule has exactly one documented carve-out."""
        security = read("SECURITY.md")
        self.assertIn("Responses-API Code Interpreter", security)
        self.assertIn(
            "Any other direct model call is a security architecture change", security
        )


if __name__ == "__main__":
    unittest.main()
