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
INFRA_WORKFLOW = (ROOT / ".github" / "workflows" / "infra-validate.yml").read_text(encoding="utf-8")
SERVICES = (ROOT / "site" / "data" / "services.js").read_text(encoding="utf-8")
STATUS_SOURCE = (ROOT / "scripts" / "status-snapshot.ps1").read_text(encoding="utf-8")
PROXY_APPCONFIG = (
    ROOT / "proxy" / "SimpleL7Proxy" / "Config" / "AppConfigService.cs"
).read_text(encoding="utf-8")
POSTPROVISION = (ROOT / "scripts" / "postprovision.ps1").read_text(encoding="utf-8")


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
    @staticmethod
    def _assert_sentinel_contract(main: str, keyvault: str, postprovision: str) -> None:
        # ARM keyValue children require pass-through auth when local auth is off.
        # The hook intentionally avoids that same-deployment RBAC propagation race.
        assert "Microsoft.AppConfiguration/configurationStores/keyValues@" not in keyvault
        assert "param appConfigDataOwnerPrincipalIds array = []" in keyvault
        assert "for pid in appConfigDataOwnerPrincipalIds" in keyvault
        assert "scope: appConfig" in keyvault
        assert "5ae67dd6-50cb-40e7-96ff-dc2bfa4b606b" in keyvault
        assert (
            "appConfigDataOwnerPrincipalIds: empty(deploymentPrincipalId) "
            "? [] : [deploymentPrincipalId]"
        ) in main
        assert "appConfigReaderPrincipalIds: [proxyIdentity.principalId]" in main
        assert "output AZURE_APP_CONFIG_LABEL string = proxyAppConfigLabel" in main
        assert "function Register-AppConfigurationSentinel" in postprovision
        assert "Get-EnvValue 'AZURE_PRINCIPAL_ID'" in postprovision
        assert "workflow-owned sentinel left unchanged" in postprovision
        assert "'--auth-mode', 'login'" in postprovision
        assert "$maxAttempts = 31" in postprovision
        assert "$retrySeconds = 30" in postprovision
        assert (
            "Add-Result -Name 'App Configuration sentinel' -Status 'FAIL'"
            in postprovision
        )

    def test_sentinel_uses_postprovision_entra_auth_and_narrow_roles(self) -> None:
        self._assert_sentinel_contract(MAIN, KEYVAULT, POSTPROVISION)
        owner_assignment = _block(
            KEYVAULT,
            r"resource appConfigDataOwnerAssignments "
            r"'Microsoft\.Authorization/roleAssignments@2022-04-01'",
        )
        self.assertIn("for pid in appConfigDataOwnerPrincipalIds", owner_assignment)
        self.assertNotIn("apiIdentity", owner_assignment)
        self.assertNotIn("proxyIdentity", owner_assignment)
        self.assertNotIn("webIdentity", owner_assignment)
        self.assertIn(
            'GetConfigurationSettingAsync("Warm:Sentinel", _labelFilter',
            PROXY_APPCONFIG,
        )

    def test_contract_detects_auth_retry_and_role_guard_mutations(self) -> None:
        mutations = (
            (MAIN.replace(
                "appConfigDataOwnerPrincipalIds: empty(deploymentPrincipalId) "
                "? [] : [deploymentPrincipalId]",
                "appConfigDataOwnerPrincipalIds: [deploymentPrincipalId]",
            ), KEYVAULT, POSTPROVISION),
            (MAIN, KEYVAULT, POSTPROVISION.replace("'--auth-mode', 'login'", "'--auth-mode', 'key'")),
            (MAIN, KEYVAULT, POSTPROVISION.replace("$maxAttempts = 31", "$maxAttempts = 1")),
            (
                MAIN,
                KEYVAULT,
                POSTPROVISION.replace(
                    "Add-Result -Name 'App Configuration sentinel' -Status 'FAIL'",
                    "Add-Result -Name 'App Configuration sentinel' -Status 'WARN'",
                ),
            ),
        )
        for mutated in mutations:
            with self.subTest(mutation=mutated):
                with self.assertRaises(AssertionError):
                    self._assert_sentinel_contract(*mutated)


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
            "services/workspaces/apis/versions/definitions@2024-06-01-preview",
            "services/workspaces/environments@2024-06-01-preview",
            "services/workspaces/apis/deployments@2024-06-01-preview",
        ):
            self.assertIn(resource_type, APICENTER)
        self.assertIn("kind: 'mcp'", APICENTER)
        self.assertNotIn("type: 'SystemAssigned'", APICENTER)
        self.assertIn("for server in servers", APICENTER)
        self.assertIn("'${normalizedGatewayBaseUrl}/${server.name}/mcp'", APICENTER)
        self.assertIn("environmentId: '/workspaces/default/environments/official-mcp-apim'", APICENTER)
        self.assertIn(
            "definitionId: '/workspaces/default/apis/${server.name}/versions/"
            "v1-preview/definitions/streamable'",
            APICENTER,
        )
        self.assertNotIn("environmentId: apimEnvironment.id", APICENTER)
        deployment = _block(
            APICENTER,
            r"resource mcpDeployments "
            r"'Microsoft\.ApiCenter/services/workspaces/apis/deployments@2024-06-01-preview'",
        )
        self.assertRegex(
            deployment,
            r"dependsOn:\s*\[\s*mcpApiDefinitions\[i\]\s*"
            r"(?://.*\s*)?apimEnvironment\s*\]",
        )
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

    def test_operator_reader_is_narrow_and_feature_gated(self) -> None:
        self.assertIn(
            "var apiCenterCatalogReaderPrincipalIds = "
            "(enablePrivateToolCatalog && !empty(deploymentPrincipalId)) "
            "? [deploymentPrincipalId] : []",
            MAIN,
        )
        self.assertIn("dataReaderPrincipalIds: apiCenterCatalogReaderPrincipalIds", MAIN)
        assignment = _block(
            APICENTER,
            r"resource apiCenterDataReaders "
            r"'Microsoft\.Authorization/roleAssignments@2022-04-01'",
        )
        self.assertIn("scope: apiCenter", assignment)
        self.assertIn("principalId: principalId", assignment)
        self.assertIn("c7244dfb-f447-457d-b2ba-3999044d1706", APICENTER)
        self.assertNotIn("subscriptionResourceId('Microsoft.ApiCenter/services'", APICENTER)

    def test_mutation_capable_contract_runs_in_always_reported_ci(self) -> None:
        self.assertIn(
            "python -m unittest scripts.tests.test_lean_azure_iac",
            INFRA_WORKFLOW,
        )
        for guarded_path in (
            '      - "scripts/tests/test_lean_azure_iac.py"',
            '      - "scripts/status-snapshot.ps1"',
            '      - "site/data/services.js"',
            '      - "proxy/SimpleL7Proxy/Config/AppConfigService.cs"',
            '      - "app/api/pyproject.toml"',
            '      - "docs/**"',
            '      - ".github/workflows/deploy.yml"',
        ):
            self.assertIn(guarded_path, INFRA_WORKFLOW)
        pull_request = INFRA_WORKFLOW.split("pull_request:", 1)[1].split("push:", 1)[0]
        self.assertNotIn("paths:", pull_request)

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