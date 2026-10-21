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

class DeployableSurfaceTests(unittest.TestCase):
    def test_retired_network_and_profile_parameters_cannot_reenter_deployment(self) -> None:
        parameters = read("infra/main.parameters.json")
        deploy = read(".github/workflows/deploy.yml")
        main = read("infra/main.bicep")
        for unreachable in (
            "vnetIsolationEnabled", "dataTierPrivate", "proxyProfilesEnabled",
            "proxyProfileProjectionJsonBase64", "proxyAsyncEnabled",
            "proxyEventHubTelemetryEnabled", "companionAppEnabled",
        ):
            with self.subTest(parameter=unreachable):
                self.assertNotIn(f'"{unreachable}"', parameters)
                self.assertNotIn(unreachable, deploy)
                self.assertNotRegex(main, rf"(?m)^param {unreachable}\b")

    def test_retired_clients_leave_offline_evaluations_and_actor_controls_in_place(self) -> None:
        for name in ("companion-image", "live-evaluations"):
            self.assertFalse((ROOT / ".github" / "workflows" / f"{name}.yml").exists())
        for name in ("live.py", "live_contracts.py", "live_http.py", "live_oracles.py"):
            self.assertFalse((ROOT / "scripts" / "evaluations" / name).exists())
        app_ci = read(".github/workflows/app-ci.yml")
        self.assertIn("python -m scripts.evaluations run", app_ci)
        self.assertIn("scripts/tests/test_behavioral_evaluations.py", app_ci)
        self.assertNotIn("AI4IA_LIVE_EVAL_TOKEN", app_ci)
        self.assertTrue((ROOT / "proxy" / "CompanionApp" / "Program.cs").is_file())
        for marker in ("canaryActor", "evaluationActor", "realtimeCanaryActor"):
            self.assertIn(marker, read("app/api/src/ai4ia_api/policy/models.py"))

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
