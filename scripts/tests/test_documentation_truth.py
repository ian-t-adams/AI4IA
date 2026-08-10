"""Semantic guards for cross-file governance, Foundry, and operator truth."""

from __future__ import annotations

import json
import re
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
            self.assertRegex(text, r"(?i)(approval|invocation|trusted)")
            for claim in stale:
                self.assertNotIn(claim, text, f"{label} restored stale claim: {claim}")

        architecture = read("docs/architecture.md")
        self.assertIn("The gate covers **both** dispatch routes.", architecture)
        self.assertIn("passes an explicit `ApprovalPolicy.off`", architecture)
        feature_docs = current_surfaces["feature docs"]
        self.assertIn("standing trust is", feature_docs)
        self.assertIn("**not invocation approval**", feature_docs)
        self.assertIn("Unattended workflows", feature_docs)

    def test_attachment_analysis_is_always_held(self) -> None:
        governance = read("app/api/src/ai4ia_api/agents/synthetic_governance.py")
        block = re.search(
            r"_ANALYZE_ATTACHMENT\s*=\s*ToolSpec\((.*?)\n\)",
            governance,
            re.DOTALL,
        )
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("risk=ToolRisk.external", block.group(1))
        self.assertNotIn("injection_only_risk", block.group(1))

        architecture = " ".join(read("docs/architecture.md").split())
        feature = " ".join(read("docs/runbooks/feature-enablement.md").split())
        roadmap = " ".join(read("docs/roadmap.md").split())
        self.assertRegex(
            architecture,
            r"held on every turn \| `browse_url`, `run_code`, `analyze_attachment`",
        )
        self.assertNotRegex(
            architecture,
            r"never held \|[^|]*`analyze_attachment`",
        )
        self.assertIn("Three capabilities prompt on every use", feature)
        self.assertIn("`browse_url`, `run_code`, and `analyze_attachment`", feature)
        self.assertIn("`run_code`, and `analyze_attachment` are held on every turn", roadmap)

    def test_foundry_lifecycle_and_cross_references_are_semantic(self) -> None:
        toolbox = json.loads(read("foundry/toolbox.manifest.json"))
        routine = json.loads(read("foundry/routines/example.routine.json"))
        a2a = json.loads(read("foundry/a2a/example.a2a.json"))

        for manifest in (toolbox, routine, a2a):
            self.assertEqual("1.0", manifest["manifestVersion"])
            self.assertTrue(manifest["owner"])
            self.assertEqual("azure-ai-projects", manifest["sdkContract"]["package"])
            self.assertEqual("2.4.0", manifest["sdkContract"]["version"])

        example_toolbox = json.loads(read("foundry/toolbox.manifest.example.json"))
        self.assertEqual("active", toolbox["lifecycle"])
        self.assertEqual("reference", example_toolbox["lifecycle"])
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
        provisioner = read("scripts/provision-foundry-toolbox.py")
        self.assertIn("lifecycle='active'", provisioner)
        workflow = read(".github/workflows/foundry-assets.yml")
        self.assertNotRegex(
            workflow,
            r"provision-foundry-toolbox\.py\s+\\?\s*--manifest",
        )

    def test_design_artifacts_do_not_claim_served_governance(self) -> None:
        surfaces = "\n".join(
            read(path)
            for path in (
                "foundry/routines/routine.schema.json",
                "foundry/routines/example.routine.json",
                "foundry/a2a/a2a.schema.json",
                "foundry/a2a/example.a2a.json",
                "scripts/provision-foundry-routine.py",
                "scripts/provision-foundry-a2a.py",
                "docs/foundry-toolbox.md",
            )
        )
        self.assertIn("DESIGN-ONLY", surfaces)
        self.assertIn("No runtime/APIM", surfaces)
        for stale in (
            r"(?i)inherits that governance",
            r"(?i)every tool call flows through",
            r"(?i)gated on APIM auth",
            r"(?i)A2A endpoint scaffold shipped",
        ):
            self.assertNotRegex(surfaces, stale)

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

    def test_foundry_endpoint_has_one_authoritative_environment_scope(self) -> None:
        standup = read("docs/runbooks/greenfield-standup.md")
        workflow = read(".github/workflows/foundry-assets.yml")
        self.assertIn("--env production --body $projectEndpoint", standup)
        self.assertIn("gh variable delete AZURE_FOUNDRY_PROJECT_ENDPOINT", standup)
        self.assertIn("gh variable get AZURE_FOUNDRY_PROJECT_ENDPOINT --env production", standup)
        self.assertNotIn("Choose one scope", standup)
        self.assertIn("environment: production", workflow)
        self.assertIn("production environment variables", workflow)
        self.assertNotIn("repository or production-environment variable", workflow)

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
            "azd env get-value AZURE_APIM_NAME",
            "azd env get-value AZURE_APIM_RESOURCE_ID",
            "az apim show -g $rg -n $apim --query id",
        ):
            self.assertIn(derived, rotation)
        self.assertIn("Those values are evidence, not reusable", rotation)
        self.assertNotIn("-n ca-proxy-slurmfactory", rotation)
        self.assertNotIn("-n ca-api-slurmfactory", rotation)
        self.assertNotIn("az apim list", rotation)
        main_bicep = read("infra/main.bicep")
        self.assertIn("output AZURE_APIM_NAME string", main_bicep)
        self.assertIn("output AZURE_APIM_RESOURCE_ID string", main_bicep)

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
        self.assertRegex(meta, r"\btemplateOn:\s*(true|false)")
        self.assertIn("templateOn", generator)
        self.assertNotRegex(generator, r"\bon:\s*(true|false)")
        renderer = read("site/assets/app.js")
        self.assertIn("m.featurePosture", renderer)
        self.assertIn("posture.observedAt", renderer)
        self.assertIn("posture.observedSource", renderer)
        self.assertIn("posture.caveat", renderer)

    def test_security_and_memory_migration_claims_stay_repaired(self) -> None:
        security = read("SECURITY.md")
        self.assertIn("Responses-API Code Interpreter", security)
        self.assertIn("Any other direct model call is a security architecture change", security)

        migration = read("docs/runbooks/memory-migration.md")
        self.assertNotIn("workflow already\n> defaults to `disabled`", migration)
        self.assertIn("normal\n> repository/parameter default is now `cosmos`", migration)
        self.assertGreaterEqual(
            migration.count("uv run --with asyncpg python"),
            2,
        )
        self.assertNotRegex(
            migration,
            r"uv run python \.\.\\\.\.\\scripts\\migrate-memory-to-cosmos\.py",
        )
        self.assertIn("intentionally absent from\n`pyproject.toml`", migration)

    def test_site_architecture_splits_canonical_and_best_effort_writes(self) -> None:
        self.assertNotIn("`n", read("site/README.md"))
        architecture = read("site/architecture.html")
        self.assertIn("Code Interpreter", architecture)
        self.assertIn("Canonical message/session writes surface failure", architecture)
        self.assertIn(
            "Usage-ledger/metering writes, telemetry, and derived stores are best-effort",
            architecture,
        )
        self.assertIn("usage ledger + telemetry (best-effort; failures logged)", architecture)
        self.assertNotIn("persist messages + usage ledger (best-effort)", architecture)

    def test_portal_requirements_and_cards_stay_repaired(self) -> None:
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
        self.assertIn("validation-only routine and A2A design artifacts", docs_manifest)

    def test_changelog_separates_merge_deploy_and_release(self) -> None:
        changelog = read("CHANGELOG.md")
        self.assertIn("a merge,", changelog)
        self.assertIn("workflow deployment, and GitHub release/tag are distinct events", changelog)

    def test_code_interpreter_service_card_names_the_direct_exception(self) -> None:
        services = read("site/data/services.js")
        self.assertIn("explicit direct model-inference exception", services)
        self.assertNotRegex(
            services,
            r"(?i)native/control planes such as CU and code interpreter",
        )

    def test_rai_evidence_is_scoped_to_text_chat(self) -> None:
        record = read("docs/rai-decision-record.md")
        self.assertIn("Implemented for text chat completions", record)
        self.assertIn("does not evidence equivalent", record)
        self.assertIn("No modality evidence", record)
        self.assertNotRegex(record, r"every category on every turn")

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

    def test_network_isolation_is_design_only_until_end_to_end(self) -> None:
        parameters = read("infra/main.parameters.json")
        deploy = read(".github/workflows/deploy.yml")
        for unreachable in ("vnetIsolationEnabled", "dataTierPrivate"):
            self.assertNotIn(f'"{unreachable}"', parameters)
            self.assertNotIn(unreachable, deploy)

        required_gaps = (
            "ACR",
            "App Configuration",
            "Search",
            "Foundry",
            "APIM",
            "monitoring",
        )
        for path in ("README.md", "docs/architecture.md", "docs/roadmap.md"):
            text = " ".join(read(path).split())
            for gap in required_gaps:
                self.assertIn(gap, text)
        architecture = read("docs/architecture.md")
        self.assertIn("No served private/regulated network mode", architecture)
        self.assertIn("design scaffolding only", architecture)
        self.assertIn("endpoint/private-DNS matrix", architecture)


if __name__ == "__main__":
    unittest.main()
