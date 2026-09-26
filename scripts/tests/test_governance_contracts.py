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
        """`analyze_attachment` needs approval even without tainted context.

        Scoped user consent can supply that approval; risk classification must
        not be weakened to avoid the per-call prompt.
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

    def test_tool_auto_approval_operator_switch_is_reachable_and_defaults_off(self) -> None:
        parameters = json.loads(read("infra/main.parameters.json"))["parameters"]
        self.assertEqual(
            parameters["toolAutoApproveEnabled"]["value"],
            "${AI4IA_TOOL_AUTO_APPROVE_ENABLED=false}",
        )
        self.assertIn(
            "AI4IA_TOOL_AUTO_APPROVE_ENABLED: ${{ vars.AI4IA_TOOL_AUTO_APPROVE_ENABLED }}",
            read(".github/workflows/deploy.yml"),
        )


class ConversationDeletionContractTests(unittest.TestCase):
    def test_opt_in_is_reachable_without_changing_existing_partition_or_retention(self) -> None:
        parameters = json.loads(read("infra/main.parameters.json"))["parameters"]
        for name, env, default in (
            ("sessionDeletionEnabled", "AI4IA_SESSION_DELETION_ENABLED", "false"),
            ("sessionDeletionRolloutId", "AI4IA_SESSION_DELETION_ROLLOUT_ID", ""),
        ):
            self.assertEqual(parameters[name]["value"], "${" + env + "=" + default + "}")
            self.assertIn(env + ": ${{ vars." + env + " }}", read(".github/workflows/deploy.yml"))
        data = read("infra/modules/data.bicep")
        for container, path in (("sessions", "userId"), ("messages", "sessionId"), ("documents", "sessionId")):
            self.assertRegex(data, rf"name: '{container}'\s+partitionKey: '/{path}'")
        shared_containers = data.split("resource cosmosContainers ", 1)[1].split(
            "resource cosmosMemoriesContainer ", 1
        )[0]
        self.assertNotIn("defaultTtl", shared_containers)


class HardQuotaActivationContractTests(unittest.TestCase):
    def test_opt_in_is_reachable_without_new_resources_or_usage_retention(self) -> None:
        parameters = json.loads(read("infra/main.parameters.json"))["parameters"]
        for name, env, default in (
            ("hardQuotaEnabled", "AI4IA_HARD_QUOTA_ENABLED", "false"),
            ("hardQuotaRolloutId", "AI4IA_HARD_QUOTA_ROLLOUT_ID", ""),
        ):
            self.assertEqual(parameters[name]["value"], "${" + env + "=" + default + "}")
            self.assertIn(env + ": ${{ vars." + env + " }}", read(".github/workflows/deploy.yml"))
        data = read("infra/modules/data.bicep")
        # The rollout record and owner documents share the existing usage
        # container; activation requires it to keep /userId and no default TTL.
        self.assertRegex(data, r"name: 'usage'\s+partitionKey: '/userId'")
        self.assertNotIn("hard_quota", data)
        self.assertNotIn("hardQuota", data)


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
                self.assertEqual("2.6.1", manifest["sdkContract"]["version"])

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


def _runbook_section(heading: str) -> str:
    runbook = read("docs/runbooks/feature-enablement.md")
    start = runbook.index(f"\n### {heading}\n")
    end = runbook.find("\n### ", start + 1)
    return runbook[start:end if end != -1 else None]


# The vocabulary every retained-object rollback in the runbook uses: Incremental
# mode keeps the objects, the key is suspended or revoked first, and deletion is
# a targeted, reviewed what-if, never complete mode.
RETAINED_ROLLBACK_TERMS = ("Incremental mode", "suspend or revoke", "what-if", "complete deployment mode")


class RetainedInventoryContractTests(unittest.TestCase):
    """A flag-off rollback must name what an earlier provision leaves behind.

    Turning a flag off under ARM Incremental mode stops managing, but never
    deletes, the objects an earlier provision created. The inventory is derived
    from the Bicep, so a new flag-gated object fails here until the runbook names
    it; the prose around it is not pinned.
    """

    RESOURCE = re.compile(
        r"^resource \w+ '(?P<type>Microsoft\.[\w./]+)@[\w.-]+' = (?:\[for \w+ in \w+: )?"
        r"if \((?P<flag>\w+)\) \{\n(?P<body>.*?)^\}",
        re.MULTILINE | re.DOTALL,
    )

    def _gated(self, module: str, flag: str) -> dict[str, str]:
        """Map each resource type gated on ``flag`` to the token a runbook names it by."""
        source = read(module)
        found: dict[str, str] = {}
        for match in self.RESOURCE.finditer(source):
            if match["flag"] != flag:
                continue
            name = re.search(r"^  name: (.+)$", match["body"], re.MULTILINE)
            self.assertIsNotNone(name, match["type"])
            assert name is not None
            value = name[1].strip()
            if value == "'policy'":
                token = "policy"
            elif value.endswith(".name"):  # one resource per loop row
                token = "operations"
            elif value.startswith("'"):
                token = value.strip("'")
            else:  # a variable or parameter holding the name
                declared = re.search(rf"^(?:var|param) {value}(?: string)? = '([^']+)'$", source, re.MULTILINE)
                self.assertIsNotNone(declared, value)
                assert declared is not None
                token = declared[1].replace("${workload}", "<workload>")
            found[match["type"]] = token
        return found

    def test_photo_avatar_rollback_names_every_object_the_flag_created(self) -> None:
        apim = self._gated("infra/modules/gateway.bicep", "photoAvatarsEnabled")
        data = self._gated("infra/modules/data.bicep", "deployPhotoAvatarStorage")
        # Non-vacuity: the derivation still sees every gated object.
        self.assertEqual(sorted(apim), sorted(
            f"Microsoft.ApiManagement/service/{kind}"
            for kind in ("namedValues", "apis", "apis/operations", "apis/policies", "subscriptions")
        ))
        self.assertEqual(len(data), 2)
        inventory = sorted({*apim.values(), *data.values()})
        section = _runbook_section("Custom photo avatars")
        rollback = section[section.index("**Degradation and rollback.**"):]
        for token in inventory:
            with self.subTest(token=token):
                self.assertIn(f"`{token}`" if token not in {"operations", "policy"} else token, rollback)
        for term in RETAINED_ROLLBACK_TERMS:
            with self.subTest(term=term):
                self.assertIn(term, rollback)
        # Control: another feature's rollback, written to the same rules, uses the
        # same vocabulary but cannot satisfy this inventory.
        voice = _runbook_section("Speech Voice Live (second voice provider)")
        for term in RETAINED_ROLLBACK_TERMS:
            self.assertIn(term, voice)
        self.assertFalse(all(f"`{token}`" in voice for token in inventory if token not in {"operations", "policy"}))

if __name__ == "__main__":
    unittest.main()
