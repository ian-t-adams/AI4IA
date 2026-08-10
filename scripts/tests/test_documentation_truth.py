"""Semantic guards for cross-file governance, Foundry, and operator truth."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class DocumentationTruthTests(unittest.TestCase):
    def test_rai_document_separates_completeness_from_modality_approval(self) -> None:
        record = read("docs/rai-decision-record.md")
        self.assertIn(
            "DOCUMENT STATUS: complete. CONTROL APPROVAL STATUS: incomplete.",
            record,
        )
        self.assertNotIn("**STATUS: complete.**", record)
        for modality in (
            "Image",
            "video",
            "Azure OpenAI Voice Live",
            "Speech Voice Live",
        ):
            self.assertIn(modality, record)
        self.assertIn("Evidence required to close the control", record)
        self.assertIn("No modality-scope approval artifact is present", record)

    def test_discovery_trust_is_not_described_as_invocation_approval(self) -> None:
        current_surfaces = {
            "feature docs": read("docs/runbooks/feature-enablement.md"),
            "site architecture": read("site/architecture.html"),
            "agent builder": read("app/web/src/components/AgentBuilder.tsx"),
            "MCP builder": read("app/web/src/components/McpServerBuilder.tsx"),
            "official service": read(
                "app/api/src/ai4ia_api/agents/official_mcp_service.py"
            ),
        }
        stale = (
            "trusted, pre-approved",
            "pre-approved, no per-call",
            "tools are pre-approved",
            "Chat has no live approval prompt",
        )
        for label, text in current_surfaces.items():
            for claim in stale:
                self.assertNotIn(claim, text, f"{label} restored stale claim: {claim}")

        architecture = read("docs/architecture.md")
        self.assertIn("The gate covers **both** dispatch routes.", architecture)
        self.assertIn("passes an explicit `ApprovalPolicy.off`", architecture)
        feature_docs = current_surfaces["feature docs"]
        self.assertIn("standing trust is", feature_docs)
        self.assertIn("**not invocation approval**", feature_docs)
        self.assertIn("Unattended workflows", feature_docs)

    def test_foundry_lifecycle_and_cross_references_are_semantic(self) -> None:
        toolbox = json.loads(read("foundry/toolbox.manifest.json"))
        routine = json.loads(read("foundry/routines/example.routine.json"))
        a2a = json.loads(read("foundry/a2a/example.a2a.json"))

        for manifest in (toolbox, routine, a2a):
            self.assertEqual("1.0", manifest["manifestVersion"])
            self.assertTrue(manifest["owner"])
            self.assertEqual("azure-ai-projects", manifest["sdkContract"]["package"])
            self.assertEqual("2.4.0", manifest["sdkContract"]["version"])

        self.assertEqual("active", toolbox["lifecycle"])
        self.assertEqual("validated", toolbox["sdkContract"]["status"])
        self.assertEqual("design-preview", routine["lifecycle"])
        self.assertEqual("not-executable", routine["sdkContract"]["status"])
        self.assertEqual("design-preview", a2a["lifecycle"])
        self.assertEqual("not-executable", a2a["sdkContract"]["status"])

        canonical_tools = {
            tool.get("name") or tool.get("serverLabel") for tool in toolbox["tools"]
        }
        referenced_tools = {
            tool for step in routine["steps"] for tool in step.get("tools", [])
        }
        self.assertTrue(referenced_tools)
        self.assertLessEqual(referenced_tools, canonical_tools)

        expected_blockers = {
            "protocol-and-version",
            "endpoint-discovery",
            "backend-authentication",
            "apim-operation",
            "apim-product-and-subscription",
            "apim-policy",
            "runtime-client-wiring",
        }
        self.assertEqual(expected_blockers, set(a2a["blockingRequirements"]))
        a2a_script = read("scripts/provision-foundry-a2a.py")
        self.assertNotIn("to_az_commands", a2a_script)
        self.assertNotIn("build_agent_link", a2a_script)
        self.assertNotIn("--emit-az", a2a_script)

        infra_validate = read(".github/workflows/infra-validate.yml")
        self.assertIn("provision-foundry-routine.py --check", infra_validate)
        self.assertIn("provision-foundry-a2a.py --check", infra_validate)

    def test_greenfield_toolbox_and_acceptance_steps_are_complete(self) -> None:
        standup = read("docs/runbooks/greenfield-standup.md")
        for required in (
            'python -m pip install -e "app/api[foundry]"',
            "azd env get-value AZURE_FOUNDRY_PROJECT_ENDPOINT",
            "gh variable set AZURE_FOUNDRY_PROJECT_ENDPOINT",
            "gh workflow run foundry-assets.yml --ref main",
            "/api/admin/metrics/official-mcp?refresh=true",
            "initialize` -> `tools/list",
            "toolCount: 3",
            "Bounded fixture",
            "Cleanup",
            "repository cannot truthfully claim live acceptance from static CI",
        ):
            self.assertIn(required, standup)

    def test_teardown_and_rotation_targets_are_truthful(self) -> None:
        teardown = read("docs/runbooks/teardown.md")
        self.assertIn("separate subscription", teardown)
        self.assertIn("Reduced-profile validation", teardown)
        self.assertIn("subscription-wide MAI quota", teardown)
        self.assertIn("Do not claim a reduced profile proves full model availability", teardown)

        rotation = read("docs/runbooks/key-rotation.md")
        for derived in (
            "$envName = azd env get-value AZURE_ENV_NAME",
            '$proxyApp = "ca-proxy-$envName"',
            '$apiApp = "ca-api-$envName"',
            '$sid = "$workload-api-proxy-ingress"',
            '$url = "https://$proxyFqdn/openai/status"',
            "az account set --subscription $sub",
        ):
            self.assertIn(derived, rotation)
        self.assertIn("Those values are evidence, not reusable", rotation)
        self.assertNotIn("-n ca-proxy-slurmfactory", rotation)
        self.assertNotIn("-n ca-api-slurmfactory", rotation)

    def test_portal_separates_defaults_from_dated_observation(self) -> None:
        meta = read("site/data/meta.js")
        self.assertIn("templateSource", meta)
        self.assertIn("observedSource", meta)
        self.assertIn("observedAt", meta)
        self.assertIn(
            'name: "Proxy priority reservations", templateOn: false, observedOn: true',
            meta,
        )
        self.assertIn(
            'name: "Speech Voice Live", templateOn: false, observedOn: true',
            meta,
        )
        generator = read("scripts/gen-docs-catalog.py")
        self.assertIn("CI deliberately does not query Azure", generator)
        self.assertIn("templateOn", generator)
        self.assertNotIn(r"\bon:\s*(true|false)", generator)
        renderer = read("site/assets/app.js")
        self.assertIn("m.featurePosture", renderer)
        self.assertIn("posture.observedAt", renderer)
        self.assertIn("posture.observedSource", renderer)
        self.assertIn("posture.caveat", renderer)

    def test_known_stale_security_and_portal_claims_stay_repaired(self) -> None:
        security = read("SECURITY.md")
        self.assertIn("Responses-API Code Interpreter", security)
        self.assertIn("Any other direct model call is a security architecture change", security)

        migration = read("docs/runbooks/memory-migration.md")
        self.assertNotIn("workflow already\n> defaults to `disabled`", migration)
        self.assertIn("normal\n> repository/parameter default is now `cosmos`", migration)

        self.assertNotIn("`n", read("site/README.md"))

        architecture = read("site/architecture.html")
        self.assertIn("Code Interpreter", architecture)
        self.assertIn("Canonical message/session writes must surface failure", architecture)
        self.assertNotIn("persist messages + usage ledger (best-effort)", architecture)

        requirements = read("site/requirements.html")
        self.assertIn("Application Insights uses its scoped connection string", requirements)
        extras = read("site/data/requirements.js")
        self.assertIn("installed by app-ci and foundry-assets", extras)
        self.assertIn("never by the runtime image", extras)

        docs_manifest = read("site/data/docs.manifest.json")
        self.assertNotIn(
            "Toolbox, skills, routine and A2A manifests validated by CI and applied",
            docs_manifest,
        )
        changelog = read("CHANGELOG.md")
        self.assertIn("a merge,", changelog)
        self.assertIn("workflow deployment, and GitHub release/tag are distinct events", changelog)

    def test_api_identity_bypass_is_recorded_as_a_pending_decision(self) -> None:
        architecture = read("docs/architecture.md")
        roadmap = read("docs/roadmap.md")
        for text in (architecture, roadmap):
            self.assertIn("nativeFoundryPrincipalIds", text)
            self.assertIn("Cognitive Services OpenAI User", text)
            self.assertIn("dedicated identity", text)
            self.assertIn("owner approval", text)
        self.assertIn("No RBAC is changed by documenting the decision", architecture)
        self.assertIn("P1 Security decision", roadmap)
        self.assertIn(
            "pending least-privilege identity-split decision",
            read("site/architecture.html"),
        )
        self.assertIn(
            "dedicated identity split is a pending owner-approved security decision",
            read("site/data/requirements.js"),
        )


if __name__ == "__main__":
    unittest.main()
