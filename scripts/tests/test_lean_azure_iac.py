"""Contract tests for the lean Azure capability map and its optional planes."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
EVENTHUBS = (ROOT / "infra" / "modules" / "eventhubs.bicep").read_text(encoding="utf-8")
KEYVAULT = (ROOT / "infra" / "modules" / "keyvault.bicep").read_text(encoding="utf-8")
MONITORING = (ROOT / "infra" / "modules" / "monitoring.bicep").read_text(encoding="utf-8")
APICENTER = (ROOT / "infra" / "modules" / "apicenter.bicep").read_text(encoding="utf-8")
FOUNDRY = (ROOT / "infra" / "modules" / "foundry.bicep").read_text(encoding="utf-8")
MAIN_PARAMETERS = (ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8")
DEPLOY_WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
SERVICES = (ROOT / "site" / "data" / "services.js").read_text(encoding="utf-8")
STATUS_SOURCE = (ROOT / "scripts" / "status-snapshot.ps1").read_text(encoding="utf-8")
PROXY_APPCONFIG = (
    ROOT / "proxy" / "SimpleL7Proxy" / "Config" / "AppConfigService.cs"
).read_text(encoding="utf-8")


def _block(text: str, start_pattern: str) -> str:
    match = re.search(start_pattern, text)
    if match is None:
        raise AssertionError(f"block not found: {start_pattern}")
    start = text.find("{", match.start())
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise AssertionError(f"unterminated block: {start_pattern}")


class EventHubsPostureTests(unittest.TestCase):
    def test_default_off_gates_the_entire_module_and_api_has_no_roles(self) -> None:
        self.assertIn("param proxyEventHubTelemetryEnabled bool = false", MAIN)
        call = _block(
            MAIN,
            r"module eventhubs 'modules/eventhubs\.bicep' = if \(proxyEventHubTelemetryEnabled\)",
        )
        self.assertIn("proxyIdentity.principalId", call)
        self.assertIn("receiverPrincipalIds: []", call)
        self.assertNotIn("apiIdentity.principalId", call)
        for capability in (
            "Microsoft.EventHub/namespaces@",
            "Microsoft.Authorization/roleAssignments@",
            "Microsoft.Insights/diagnosticSettings@",
        ):
            self.assertIn(capability, EVENTHUBS)

    def test_gateway_inputs_and_operator_outputs_are_safe_when_disabled(self) -> None:
        for name, output in (
            ("eventHubNamespaceFqdn", "namespaceFqdn"),
            ("eventHubName", "telemetryHubName"),
        ):
            self.assertRegex(
                MAIN,
                rf"{name}:\s*proxyEventHubTelemetryEnabled\s*\?\s*"
                rf"eventhubs\.outputs\.{output}\s*:\s*''",
            )
        self.assertRegex(
            MAIN,
            r"output AZURE_EVENTHUBS_NAMESPACE_FQDN string = "
            r"proxyEventHubTelemetryEnabled \? eventhubs\.outputs\.namespaceFqdn : ''",
        )
        self.assertRegex(
            MAIN,
            r"output AZURE_EVENTHUBS_TELEMETRY_HUB string = "
            r"proxyEventHubTelemetryEnabled \? eventhubs\.outputs\.telemetryHubName : ''",
        )


class ActiveConfigurationTests(unittest.TestCase):
    def test_label_aware_sentinel_matches_the_proxy_refresh_contract(self) -> None:
        self.assertIn(
            "Microsoft.AppConfiguration/configurationStores/keyValues@2024-05-01",
            KEYVAULT,
        )
        self.assertIn("'Warm:Sentinel$${appConfigLabel}'", KEYVAULT)
        self.assertIn("value: 'ready'", KEYVAULT)
        self.assertIn("appConfigLabel: proxyAppConfigLabel", MAIN)
        self.assertIn(
            'GetConfigurationSettingAsync("Warm:Sentinel", _labelFilter',
            PROXY_APPCONFIG,
        )


class LeanMonitoringTests(unittest.TestCase):
    def test_unused_prometheus_workspace_is_absent_everywhere_authoritative(self) -> None:
        all_bicep = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "infra").rglob("*.bicep")
        )
        self.assertNotIn("Microsoft.Monitor/accounts", all_bicep)
        self.assertNotIn("monitorWorkspaceId", all_bicep)
        self.assertNotIn("Azure Monitor Workspace", SERVICES)
        self.assertIn("Retained Monitor Workspace (not in IaC)", STATUS_SOURCE)
        self.assertIn("WorkspaceResourceId: logAnalytics.id", MONITORING)


class DefenderEventingTests(unittest.TestCase):
    def test_event_grid_is_presented_as_active_defender_security_eventing(self) -> None:
        start = SERVICES.index('key: "eventgrid"')
        event_grid = SERVICES[start : SERVICES.index("  },", start)]
        for phrase in (
            "Defender for Storage V2",
            "StorageAntimalwareSubscription",
            "On-Upload Malware Scanning",
            "Sensitive Data Discovery",
            "not app-ingestion placeholders",
        ):
            self.assertIn(phrase, event_grid)
        self.assertIn("Defender for Storage Event Topic", STATUS_SOURCE)
        self.assertIn("group = 'Security'", STATUS_SOURCE)


class FoundryAssetProvisioningRoleTests(unittest.TestCase):
    def test_deployment_principal_is_gated_for_toolbox_asset_reconciliation(self) -> None:
        self.assertIn(
            "var foundryToolboxDeploymentPrincipal = "
            "(enableFoundryToolbox && !empty(deploymentPrincipalId)) "
            "? [deploymentPrincipalId] : []",
            MAIN,
        )
        self.assertIn(
            "union(foundryToolboxApimPrincipal, foundryToolboxDeploymentPrincipal)",
            MAIN,
        )
        self.assertIn(
            "toolboxPrincipalIds: (i == primaryFoundryIndex) "
            "? foundryToolboxPrincipalIds : []",
            MAIN,
        )

    def test_workflow_supplies_the_principal_object_id_not_the_client_id(self) -> None:
        self.assertIn('"${AZURE_PRINCIPAL_ID=}"', MAIN_PARAMETERS)
        self.assertIn(".principalId", DEPLOY_WORKFLOW)
        self.assertIn("AZURE_PRINCIPAL_ID=$principal_id", DEPLOY_WORKFLOW)
        self.assertNotIn("[AZURE_CLIENT_ID]", MAIN)
        self.assertNotIn("[clientId]", MAIN)

    def test_role_is_foundry_user_at_primary_project_scope(self) -> None:
        assignment = _block(
            FOUNDRY,
            r"resource toolboxFoundryUserAssignments "
            r"'Microsoft\.Authorization/roleAssignments@2022-04-01'",
        )
        self.assertIn("for pid in toolboxPrincipalIds", assignment)
        self.assertIn("scope: project", assignment)
        self.assertIn("principalId: pid", assignment)
        self.assertIn(
            "var foundryUserRoleId = "
            "'53ca6127-db72-4b80-b1b0-d745d6d5456d'",
            FOUNDRY,
        )


class ApiCenterCatalogTests(unittest.TestCase):
    def test_every_official_server_is_an_iac_owned_apim_deployment(self) -> None:
        servers = json.loads((ROOT / "infra" / "mcp-servers.json").read_text(encoding="utf-8"))[
            "servers"
        ]
        self.assertGreater(len(servers), 0, "the activated catalog must not pass vacuously")
        for resource_type in (
            "services/workspaces/apis@2024-06-01-preview",
            "services/workspaces/apis/versions@2024-06-01-preview",
            "services/workspaces/environments@2024-06-01-preview",
            "services/workspaces/apis/deployments@2024-06-01-preview",
        ):
            self.assertIn(resource_type, APICENTER)
        self.assertIn("kind: 'mcp'", APICENTER)
        self.assertNotIn("type: 'SystemAssigned'", APICENTER)
        self.assertIn("for server in servers", APICENTER)
        self.assertIn("'${normalizedGatewayBaseUrl}/${server.name}/mcp'", APICENTER)
        self.assertIn("state: 'active'", APICENTER)
        call = _block(MAIN, r"module apicenter 'modules/apicenter\.bicep'")
        self.assertIn("if (enablePrivateToolCatalog && enableOfficialMcp)", call)
        self.assertIn(
            "(enablePrivateToolCatalog && enableOfficialMcp) ? "
            "apicenter.outputs.apiCenterName : ''",
            MAIN,
        )
        self.assertIn("servers: officialMcpServers", call)
        self.assertIn("gatewayBaseUrl: apimcore.outputs.gatewayUrl", call)

    def test_manual_half_wiring_and_sdk_are_retired(self) -> None:
        self.assertFalse((ROOT / "scripts" / "provision-private-tool-catalog.py").exists())
        pyproject = (ROOT / "app" / "api" / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("azure-mgmt-apicenter", pyproject)
        docs = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "docs").rglob("*.md")
        )
        self.assertNotIn("provision-private-tool-catalog.py", docs)


if __name__ == "__main__":
    unittest.main()