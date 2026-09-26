"""Compile Bicep and pin warning-free, behavior-preserving ARM output.

The tests invoke the same pinned compiler installed by infra-validate. They inspect
the generated ARM template so source-only edits cannot claim safety while changing
the conditional outputs or endpoint normalization that Azure evaluates.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.tests._production_fixture import production_document

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "infra" / "main.bicep"
MODELS = ROOT / "infra" / "models.json"


def _compile(main: Path = MAIN) -> subprocess.CompletedProcess[str]:
    bicep = shutil.which("bicep")
    if bicep:
        command = [bicep, "build", str(main), "--stdout"]
    else:
        az = shutil.which("az")
        if not az:
            raise AssertionError(
                "Bicep clean-diagnostics tests require standalone `bicep` or Azure CLI `az`."
            )
        command = [az, "bicep", "build", "--file", str(main), "--stdout"]
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


class BicepCompiledBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = _compile()
        if cls.build.returncode != 0:
            raise AssertionError(
                f"Bicep compilation failed ({cls.build.returncode}):\n{cls.build.stderr}"
            )
        cls.template = json.loads(cls.build.stdout)

    def test_build_has_no_bicep_diagnostics(self) -> None:
        # Azure CLI can print a version-update notice. A compiler diagnostic is a
        # source-positioned Warning/Error and must fail even though `bicep build`
        # normally exits zero for warnings.
        diagnostics = re.findall(
            r"(?im)^.*\(\d+,\d+\)\s*:\s*(?:warning|error)\s+\S+.*$",
            self.build.stderr,
        )
        self.assertEqual(
            diagnostics,
            [],
            "Bicep emitted diagnostics despite a successful exit code:\n"
            + "\n".join(diagnostics),
        )

    def test_location_allowlist_matches_catalog_primary_regions(self) -> None:
        models = json.loads(MODELS.read_text(encoding="utf-8"))
        expected = sorted(
            name
            for name, config in models["regions"].items()
            if config.get("primary") is True
        )
        actual = sorted(self.template["parameters"]["location"]["allowedValues"])
        self.assertEqual(actual, expected)
        self.assertIn("swedencentral", actual)

    def test_web_explicit_probe_list_preserves_backend_health_probes(self) -> None:
        def container(module: str) -> dict:
            resources = self.template["resources"][module]["properties"]["template"]["resources"]
            rows = resources.values() if isinstance(resources, dict) else resources
            apps = [row for row in rows if row["type"] == "Microsoft.App/containerApps"]
            self.assertEqual(len(apps), 1)
            containers = apps[0]["properties"]["template"]["containers"]
            self.assertEqual(len(containers), 1)
            return containers[0]

        web = container("web")
        self.assertIn("probes", web)
        self.assertEqual(web["probes"], [])
        for module, expected in (
            ("api", {"Liveness": "/health/live", "Readiness": "/health/ready"}),
            ("gateway", {"Startup": "/startup", "Liveness": "/liveness", "Readiness": "/readiness"}),
        ):
            with self.subTest(module=module):
                probes = container(module)["probes"]
                self.assertEqual(len(probes), len(expected))
                self.assertEqual(
                    {probe["type"]: probe["httpGet"]["path"] for probe in probes}, expected,
                )
                self.assertTrue(all(probe["httpGet"]["port"] == 8080 for probe in probes))

    def test_capacity_profile_defaults_and_real_module_selection(self) -> None:
        parameter = self.template["parameters"]["modelCapacityProfile"]
        self.assertEqual(parameter["defaultValue"], "baseline")
        self.assertEqual(parameter["allowedValues"], ["baseline", "production", "maximum"])
        inputs = [
            row["input"] for row in self.template["variables"]["copy"]
            if row["name"] == "modelDeploymentsByRegion"
        ]
        self.assertEqual(len(inputs), 1)
        self.assertIn("__bicep.selectedCapacity", inputs[0])
        self.assertIn("parameters('modelCapacityProfile')", inputs[0])
        model_module = self.template["resources"]["modelDeployments"]["properties"]
        self.assertEqual(
            model_module["parameters"]["deployments"]["value"],
            "[variables('modelDeploymentsByRegion')[copyIndex()]]",
        )
        resources = model_module["template"]["resources"]
        deployment = next(r for r in resources if r["type"] == "Microsoft.CognitiveServices/accounts/deployments")
        self.assertIn("capacity", deployment["sku"]["capacity"])

    def test_claude_entitlement_defaults_off(self) -> None:
        self.assertFalse(self.template["parameters"]["claudeEnabled"]["defaultValue"])
        self.assertFalse(self.template["parameters"]["claudeExternalEnabled"]["defaultValue"])
        self.assertEqual(self.template["parameters"]["claudeBindingJson"]["defaultValue"], "")
        self.assertIn("deployableCatalog", self.template["variables"])
        self.assertIn(
            "__bicep.deploymentTarget",
            json.dumps(self.template["variables"]["deployableCatalog"]),
        )
        self.assertIn("not(equals(lambdaVariables('model').format, 'Anthropic'))", self.template["variables"]["deployableCatalog"])

    def test_claude_operator_units_compile_default_off_without_application_or_key_resources(self) -> None:
        for filename, gate, allowed in (
            ("claude-target.bicep", "provisionClaude", {
                "Microsoft.CognitiveServices/accounts", "Microsoft.Resources/deployments",
            }),
            ("claude-identity.bicep", "createIdentity", {"Microsoft.ManagedIdentity/userAssignedIdentities"}),
            ("claude-access.bicep", "grantInferenceAccess", {
                "Microsoft.Authorization/roleDefinitions", "Microsoft.Authorization/roleAssignments",
            }),
        ):
            with self.subTest(filename=filename):
                result = _compile(ROOT / "infra" / filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotRegex(result.stderr, r"\(\d+,\d+\)\s*:\s*(?:Warning|Error)")
                template = json.loads(result.stdout)
                self.assertIs(template["parameters"][gate]["defaultValue"], False)
                resources = template["resources"]
                rows = list(resources.values()) if isinstance(resources, dict) else resources
                self.assertEqual({row["type"] for row in rows}, allowed)
                self.assertTrue(all(row["condition"] == f"[parameters('{gate}')]" for row in rows))
                if filename == "claude-target.bicep":
                    network = template["parameters"]["networkMode"]
                    self.assertNotIn("defaultValue", network)
                    self.assertEqual(network["allowedValues"], ["public-keyless"])
                    account = next(row for row in rows if row["type"] == "Microsoft.CognitiveServices/accounts")
                    self.assertIs(account["properties"]["disableLocalAuth"], True)
                if filename == "claude-access.bicep":
                    role = next(row for row in rows if row["type"] == "Microsoft.Authorization/roleDefinitions")
                    self.assertEqual(role["properties"]["permissions"], [{
                        "actions": [], "notActions": [],
                        "dataActions": ["Microsoft.CognitiveServices/accounts/AIServices/*"], "notDataActions": [],
                    }])

    def test_versioned_gateway_has_only_conditional_exact_operations_and_api_only_key(self) -> None:
        flag = "gatewayAttemptsV1Staged"
        self.assertIs(self.template["parameters"][flag]["defaultValue"], False)
        gateway_module = self.template["resources"]["gateway"]["properties"]
        api_module = self.template["resources"]["api"]["properties"]
        for module in (gateway_module, api_module):
            self.assertEqual(module["parameters"][flag]["value"], f"[parameters('{flag}')]")
            self.assertIs(module["template"]["parameters"][flag]["defaultValue"], False)
        gateway = gateway_module["template"]
        resources = gateway["resources"]
        expected = {
            "sharedAttemptsApi": "Microsoft.ApiManagement/service/apis",
            "sharedAttemptsOperations": "Microsoft.ApiManagement/service/apis/operations",
            "sharedAttemptsApiPolicy": "Microsoft.ApiManagement/service/apis/policies",
            "sharedProxyAttemptsSubscription": "Microsoft.ApiManagement/service/subscriptions",
        }
        staged = {name: value for name, value in resources.items() if flag in value.get("condition", "")}
        self.assertEqual(set(staged), set(expected))
        for name, kind in expected.items():
            self.assertEqual(resources[name]["type"], kind)
            self.assertEqual(resources[name]["condition"], f"[parameters('{flag}')]")
        self.assertTrue(resources["sharedApim"]["existing"])
        self.assertEqual(resources["sharedAttemptsApi"]["properties"], {
            "displayName": "AI4IA versioned one-attempt model boundary",
            "path": "ai4ia-attempts-v1", "protocols": ["https"],
            "serviceUrl": "[variables('foundryOpenAiUrl')]",
            "subscriptionRequired": True, "apiType": "http",
        })
        operations = gateway["variables"]["attemptsOperations"]
        self.assertEqual(operations, [
            {"name": "responses", "path": "/openai/responses", "deployment": False},
            {"name": "chat-completions", "path": "/openai/deployments/{deployment}/chat/completions", "deployment": True},
            {"name": "embeddings", "path": "/openai/deployments/{deployment}/embeddings", "deployment": True},
        ])
        operation = resources["sharedAttemptsOperations"]
        self.assertEqual(operation["copy"]["count"], "[length(variables('attemptsOperations'))]")
        self.assertEqual(operation["properties"]["method"], "POST")
        self.assertEqual(
            operation["properties"]["urlTemplate"], "[variables('attemptsOperations')[copyIndex()].path]",
        )
        self.assertIn("ai4ia-attempts-v1", operation["name"])
        subscription = resources["sharedProxyAttemptsSubscription"]
        self.assertIn("ai4ia-attempts-v1", subscription["properties"]["scope"])
        self.assertNotEqual(subscription["properties"]["scope"], resources["sharedProxyModelSubscription"]["properties"]["scope"])
        self.assertIn("sharedAttemptsApiPolicy", subscription["dependsOn"])
        self.assertIn("sharedAttemptsOperations", resources["sharedAttemptsApiPolicy"]["dependsOn"])
        self.assertEqual(
            resources["sharedAttemptsApiPolicy"]["properties"]["value"], "[variables('attemptsApiPolicyValue')]",
        )
        self.assertIn("proxyAttemptsSubscriptionName", gateway["variables"]["attemptsApiPolicyValue"])
        self.assertIn("normalizedModelPolicyFragmentDefinitions", gateway["variables"]["attemptsApiPolicyValue"])
        policy_input = re.search(r"replace\(variables\('([^']+)'\), '__AI4IA_ATTEMPTS_SUBSCRIPTION_ID__'", gateway["variables"]["attemptsApiPolicyValue"])
        self.assertIsNotNone(policy_input)
        deployed_policy = gateway["variables"][policy_input[1]]
        self.assertIn("ai4ia-attempts-v1", deployed_policy)
        self.assertIn("__AI4IA_ATTEMPTS_SUBSCRIPTION_ID__", deployed_policy)
        self.assertNotIn("<base", deployed_policy)
        for enabled in (False, True):
            # All four emitted conditions are the exact parameter, not a second
            # independently interpreted flag or an always-created child.
            count = sum((3 if name == "sharedAttemptsOperations" else 1) for name in staged if enabled)
            self.assertEqual(count, 6 if enabled else 0)
        host_env = gateway["variables"]["hostEnv"]
        self.assertIn(f"if(parameters('{flag}')", host_env)
        self.assertIn("path=/ai4ia-attempts-v1;stripprefix=false", host_env)
        self.assertIn("mode=apim;probe=/;processor=OpenAI", host_env)
        self.assertRegex(host_env, r"'Host2', 'value', format\('[^']*;retryafter=false'")
        self.assertIn("Host2-api-key", host_env)
        secrets = resources["proxyApp"]["properties"]["configuration"]["secrets"]
        self.assertIn(f"if(parameters('{flag}')", secrets)
        self.assertIn("proxy-apim-attempts-v1-key", secrets)
        self.assertIn("listSecrets(", secrets)
        self.assertNotIn("proxy-apim-attempts-v1-key", json.dumps(api_module))

    def test_photo_avatar_gateway_is_default_off_exact_and_proxy_scoped(self) -> None:
        flag = "photoAvatarsEnabled"
        self.assertIs(self.template["parameters"][flag]["defaultValue"], False)
        for limit in ("photoAvatarMaxPerUser", "photoAvatarMaxCreationsPerDay"):
            parameter = self.template["parameters"][limit]
            self.assertEqual(
                (parameter["defaultValue"], parameter["minValue"], parameter["maxValue"]), (5, 1, 50),
            )
        for limit, bounds in (
            ("photoAvatarLiveMaxMinutesPerSession", (10, 1, 60)),
            ("photoAvatarLiveIdleTimeoutSeconds", (120, 30, 900)),
        ):
            parameter = self.template["parameters"][limit]
            self.assertEqual(
                (parameter["defaultValue"], parameter["minValue"], parameter["maxValue"]), bounds,
            )
        gateway_module = self.template["resources"]["gateway"]["properties"]
        self.assertEqual(gateway_module["parameters"][flag]["value"], f"[parameters('{flag}')]")
        gateway = gateway_module["template"]
        self.assertIs(gateway["parameters"][flag]["defaultValue"], False)
        resources = gateway["resources"]
        expected = {
            "photoAvatarProjectValue": "Microsoft.ApiManagement/service/namedValues",
            "sharedPhotoAvatarApi": "Microsoft.ApiManagement/service/apis",
            "sharedPhotoAvatarOperations": "Microsoft.ApiManagement/service/apis/operations",
            "sharedPhotoAvatarApiPolicy": "Microsoft.ApiManagement/service/apis/policies",
            "sharedProxyPhotoAvatarSubscription": "Microsoft.ApiManagement/service/subscriptions",
        }
        gated = {name for name, value in resources.items() if flag in value.get("condition", "")}
        self.assertEqual(gated, set(expected))
        for name, kind in expected.items():
            self.assertEqual(resources[name]["type"], kind)
            self.assertEqual(resources[name]["condition"], f"[parameters('{flag}')]")
        api = resources["sharedPhotoAvatarApi"]["properties"]
        self.assertEqual(api["path"], "ai4ia-photo-avatars-v1")
        self.assertIs(api["subscriptionRequired"], True)
        self.assertEqual(api["protocols"], ["https"])
        self.assertEqual(gateway["variables"]["photoAvatarOperations"], [
            {"name": "photo-avatar-features", "method": "GET", "path": "/features", "avatar": False},
            {"name": "photo-avatar-project-read", "method": "GET", "path": "/project", "avatar": False},
            {"name": "photo-avatar-project-create", "method": "PUT", "path": "/project", "avatar": False},
            {"name": "photo-avatar-create", "method": "PUT", "path": "/photoavatars/{avatarId}", "avatar": True},
            {"name": "photo-avatar-read", "method": "GET", "path": "/photoavatars/{avatarId}", "avatar": True},
            {"name": "photo-avatar-delete", "method": "DELETE", "path": "/photoavatars/{avatarId}", "avatar": True},
        ])
        operation = resources["sharedPhotoAvatarOperations"]
        self.assertEqual(operation["copy"]["count"], "[length(variables('photoAvatarOperations'))]")
        self.assertNotIn("*", json.dumps(gateway["variables"]["photoAvatarOperations"]))
        policy = resources["sharedPhotoAvatarApiPolicy"]["properties"]["value"]
        self.assertIn("'__AI4IA_PHOTO_AVATAR_SUBSCRIPTION_ID__', variables('proxyPhotoAvatarSubscriptionName')", policy)
        subscription = resources["sharedProxyPhotoAvatarSubscription"]
        self.assertIn("ai4ia-photo-avatars-v1", subscription["properties"]["scope"])
        self.assertIs(subscription["properties"]["allowTracing"], False)
        self.assertNotEqual(
            subscription["properties"]["scope"], resources["sharedProxyModelSubscription"]["properties"]["scope"],
        )
        self.assertIn("sharedPhotoAvatarApiPolicy", json.dumps(subscription["dependsOn"]))
        # The proxy holds the only key, under a named host that survives an absent Host2.
        host_env = gateway["variables"]["hostEnv"]
        self.assertIn(f"if(parameters('{flag}')", host_env)
        self.assertIn("'Host-photoavatars'", host_env)
        self.assertIn("path=/ai4ia-photo-avatars-v1;stripprefix=false", host_env)
        self.assertRegex(host_env, r"'Host-photoavatars', 'value', format\('[^']*probe=/;[^']*retryafter=false'")
        self.assertNotIn("'Host3'", host_env)
        secrets = resources["proxyApp"]["properties"]["configuration"]["secrets"]
        self.assertIn("proxy-apim-photo-avatars-key", secrets)
        api_module = json.dumps(self.template["resources"]["api"])
        self.assertNotIn("proxy-apim-photo-avatars-key", api_module)
        self.assertNotIn("photoAvatarSubscription", api_module)
        # The home account and project come from the catalog, never a literal.
        self.assertIn(".photoAvatars.homeRegion", self.template["variables"]["photoAvatarHomeRegion"])
        self.assertIn("variables('photoAvatarHomeRegion')", self.template["variables"]["photoAvatarIndex"])
        self.assertIn(
            "variables('photoAvatarIndex')", gateway_module["parameters"]["photoAvatarProjectName"]["value"],
        )

    def test_photo_avatar_storage_and_api_settings_follow_the_flag(self) -> None:
        data_module = self.template["resources"]["data"]["properties"]
        self.assertEqual(
            data_module["parameters"]["deployPhotoAvatarStorage"]["value"], "[parameters('photoAvatarsEnabled')]",
        )
        data = data_module["template"]

        def one(kind: str, marker: str) -> dict:
            (match,) = [r for r in data["resources"] if r["type"] == kind and marker in r["name"]]
            return match

        container = one("Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers", "'photoAvatars'")
        self.assertEqual(container["condition"], "[parameters('deployPhotoAvatarStorage')]")
        resource = container["properties"]["resource"]
        self.assertEqual(resource["partitionKey"]["paths"], ["/userId"])
        self.assertEqual(resource["defaultTtl"], -1)
        blob = one(
            "Microsoft.Storage/storageAccounts/blobServices/containers", "parameters('photoAvatarBlobContainer')",
        )
        self.assertEqual(blob["condition"], "[parameters('deployPhotoAvatarStorage')]")
        self.assertEqual(blob["properties"]["publicAccess"], "None")
        self.assertIn("parameters('deployPhotoAvatarStorage')", data["variables"]["deployMediaStorage"])
        api = self.template["resources"]["api"]["properties"]["template"]
        env = api["variables"]["photoAvatarEnv"]
        self.assertIn(
            "createObject('name', 'AI4IA_PHOTO_AVATARS_ENABLED', 'value', string(parameters('photoAvatarsEnabled')))",
            env,
        )
        self.assertIn("if(parameters('photoAvatarsEnabled')", env)
        for name in (
            "AI4IA_PHOTO_AVATAR_BLOB_ACCOUNT_URL", "AI4IA_PHOTO_AVATAR_BLOB_CONTAINER",
            "AI4IA_PHOTO_AVATAR_MAX_PER_USER", "AI4IA_PHOTO_AVATAR_MAX_CREATIONS_PER_DAY",
            "AI4IA_PHOTO_AVATAR_LIVE_MAX_MINUTES_PER_SESSION",
            "AI4IA_PHOTO_AVATAR_LIVE_IDLE_TIMEOUT_SECONDS",
        ):
            self.assertIn(name, env)
        self.assertIn("variables('photoAvatarEnv')", api["variables"]["apiEnv"])

    def _companion(self) -> tuple[dict, dict[str, list[dict]]]:
        module = self.template["resources"]["companion"]
        resources = module["properties"]["template"]["resources"]
        rows = resources.values() if isinstance(resources, dict) else resources
        by_type: dict[str, list[dict]] = {}
        for row in rows:
            by_type.setdefault(row["type"], []).append(row)
        return module, by_type

    def test_companion_console_is_default_off_and_never_created_open(self) -> None:
        parameters = self.template["parameters"]
        self.assertIs(parameters["companionAppEnabled"]["defaultValue"], False)
        self.assertEqual(parameters["companionAppImage"]["defaultValue"], "")
        module, _ = self._companion()
        self.assertEqual(module["condition"], "[variables('companionAppDeployable')]")
        deployable = self.template["variables"]["companionAppDeployable"]
        # Every prerequisite is part of the one condition; an empty admin set, a
        # missing sign-in app or a non-digest image never creates the app.
        for guard in (
            "parameters('companionAppEnabled')",
            "parameters('proxyEventHubTelemetryEnabled')",
            "contains(parameters('companionAppImage'), '@sha256:')",
            "not(empty(parameters('companionAppEntraClientId')))",
            "greater(length(concat(variables('companionAdminGroupIds'), "
            "variables('companionAdminPrincipalIds'))), 0)",
        ):
            self.assertIn(guard, deployable)

    def test_companion_console_identity_is_read_only_and_hub_scoped(self) -> None:
        _, by_type = self._companion()
        roles = by_type["Microsoft.Authorization/roleAssignments"]
        # An assignment without an explicit scope lands on the resource group.
        scopes = {role["properties"]["roleDefinitionId"]: role.get("scope", "<resource group>") for role in roles}
        self.assertEqual(len(scopes), len(roles), "duplicate role definitions")
        module_vars = self.template["resources"]["companion"]["properties"]["template"]["variables"]
        self.assertEqual(module_vars["acrPullRoleId"], "7f951dda-4ed3-4680-a7ca-43fe172d538d")
        self.assertEqual(module_vars["eventHubsDataReceiverRoleId"], "a638d3c7-ab3a-418d-83e6-5f17a39d4fde")
        self.assertEqual(set(scopes), {
            "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('acrPullRoleId'))]",
            "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
            "variables('eventHubsDataReceiverRoleId'))]",
        })
        receiver = scopes[
            "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
            "variables('eventHubsDataReceiverRoleId'))]"
        ]
        self.assertIn("Microsoft.EventHub/namespaces/eventhubs'", receiver)
        module = json.dumps(self.template["resources"]["companion"])
        # No write, model, secret or configuration authority: Data Owner/Reader,
        # Cognitive Services, Foundry and Key Vault roles are all absent.
        for role in (
            "5ae67dd6-50cb-40e7-96ff-dc2bfa4b606b", "516239f1-63e1-4d78-a4de-a74fb236a071",
            "a97b65f3-24c7-4388-baec-2e87135dc908", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
            "53ca6127-db72-4b80-b1b0-d745d6d5456d", "4633458b-17de-408a-b874-0445c86b69e6",
            "2b629674-e913-4c01-ae53-ef4638d8f975",
        ):
            self.assertNotIn(role, module)
        self.assertEqual(len(by_type["Microsoft.EventHub/namespaces/eventhubs/consumergroups"]), 1)

    def test_companion_console_requires_entra_admin_sign_in(self) -> None:
        _, by_type = self._companion()
        (auth,) = by_type["Microsoft.App/containerApps/authConfigs"]
        properties = auth["properties"]
        self.assertIs(properties["platform"]["enabled"], True)
        self.assertEqual(properties["globalValidation"]["unauthenticatedClientAction"], "RedirectToLoginPage")
        self.assertIs(properties["httpSettings"]["requireHttps"], True)
        self.assertIs(properties["login"]["tokenStore"]["enabled"], False)
        entra = properties["identityProviders"]["azureActiveDirectory"]
        self.assertIs(entra["enabled"], True)
        self.assertNotIn("clientSecretSettingName", entra["registration"])
        policy = entra["validation"]["defaultAuthorizationPolicy"]
        self.assertEqual(policy["allowedPrincipals"], {
            "groups": "[parameters('adminGroupIds')]",
            "identities": "[parameters('adminPrincipalIds')]",
        })
        self.assertEqual(policy["allowedApplications"], ["[parameters('entraClientId')]"])

    def test_companion_console_is_not_an_azd_service_and_holds_no_secrets(self) -> None:
        _, by_type = self._companion()
        (app,) = by_type["Microsoft.App/containerApps"]
        self.assertEqual(app["tags"], "[parameters('tags')]")
        self.assertNotIn("azd-service-name", json.dumps(app))
        configuration = app["properties"]["configuration"]
        self.assertNotIn("secrets", configuration)
        ingress = configuration["ingress"]
        self.assertIs(ingress["external"], True)
        self.assertIs(ingress["allowInsecure"], False)
        (restrictions,) = ingress["copy"]
        self.assertEqual(restrictions["name"], "ipSecurityRestrictions")
        self.assertEqual(restrictions["count"], "[length(parameters('allowedIpRanges'))]")
        self.assertEqual(restrictions["input"]["action"], "Allow")
        template = app["properties"]["template"]
        self.assertEqual(template["scale"]["maxReplicas"], 1)
        (container,) = template["containers"]
        self.assertEqual(container["image"], "[parameters('image')]")
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["AZURE_TOKEN_CREDENTIALS"], "ManagedIdentityCredential")
        self.assertEqual(env["CompanionApp__EventHubMonitor__eventhub_enabled"], "true")
        # The in-app admin gate receives exactly the platform policy's allow-list.
        self.assertEqual(env["CompanionApp__Admin__GroupIds"], "[join(parameters('adminGroupIds'), ',')]")
        self.assertEqual(env["CompanionApp__Admin__PrincipalIds"], "[join(parameters('adminPrincipalIds'), ',')]")
        for forbidden in ("EVENTHUB_CONNECTIONSTRING", "CompanionApp__EventHubMonitor__ConnectionString",
                          "CompanionApp__EventHubMonitor__CheckpointStorage"):
            self.assertNotIn(forbidden, env)

    def test_proxy_hosts_and_header_policy_keep_upstream_caller_controls_off(self) -> None:
        gateway = self.template["resources"]["gateway"]["properties"]["template"]
        host_env = gateway["variables"]["hostEnv"]
        # Since the b0066b0e refresh, retryafter=true lets one tracked 5xx carrying
        # APIM's retry-after-ms block the only catch-all host, and every model with it.
        self.assertRegex(host_env, r"'Host1', 'value', format\('[^']*;retryafter=false'")
        self.assertNotIn("retryafter=true", host_env)
        static_env = json.dumps(gateway["variables"]["staticEnv"])
        self.assertIn(
            '"name": "DisallowedHeaders", "value": '
            "\"[string(createArray('S7P-Model-Override', 'S7PDEBUGBODY', 'S7PDEBUGSTREAM'))]\"",
            static_env,
        )

    def test_versioned_prefix_cannot_resolve_to_any_legacy_gateway_api(self) -> None:
        resources = self.template["resources"]["gateway"]["properties"]["template"]["resources"]
        apis = {
            name: value["properties"]["path"] for name, value in resources.items()
            if value["type"] == "Microsoft.ApiManagement/service/apis"
        }
        self.assertEqual(set(apis.values()), {
            "openai", "openai/realtime", "openai/v1/realtime",
            "code-interpreter", "speech/voice-live/realtime", "ai4ia-attempts-v1",
            "ai4ia-photo-avatars-v1",
        })
        for name, path in apis.items():
            self.assertTrue(path and "*" not in path)
            if name != "sharedAttemptsApi":
                self.assertFalse("ai4ia-attempts-v1/openai/responses".startswith(path + "/"))
            if name != "sharedPhotoAvatarApi":
                self.assertFalse("ai4ia-photo-avatars-v1/photoavatars".startswith(path + "/"))
        wildcard = resources["sharedModelOperations"]
        self.assertEqual(wildcard["properties"]["urlTemplate"], "/{*path}")
        self.assertNotIn("ai4ia-attempts-v1", wildcard["name"])

    def test_tool_auto_approval_is_an_explicit_default_off_api_gate(self) -> None:
        self.assertFalse(
            self.template["parameters"]["toolAutoApproveEnabled"]["defaultValue"]
        )
        module = self.template["resources"]["api"]["properties"]
        self.assertEqual(
            module["parameters"]["toolAutoApproveEnabled"]["value"],
            "[parameters('toolAutoApproveEnabled')]",
        )
        api = module["template"]
        self.assertFalse(api["parameters"]["toolAutoApproveEnabled"]["defaultValue"])
        self.assertEqual(
            api["variables"]["toolApprovalEnv"],
            [
                {
                    "name": "AI4IA_TOOL_AUTO_APPROVE_ENABLED",
                    "value": "[string(parameters('toolAutoApproveEnabled'))]",
                }
            ],
        )
        self.assertIn(
            "variables('toolApprovalEnv')",
            json.dumps(api["variables"]["apiEnv"]),
        )

    def test_resumable_deletion_defaults_off_and_reaches_api(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        for name, default in (
            ("sessionDeletionEnabled", False), ("sessionDeletionRolloutId", "")
        ):
            self.assertEqual(self.template["parameters"][name]["defaultValue"], default)
            self.assertEqual(api["parameters"][name]["defaultValue"], default)
            self.assertEqual(module["parameters"][name]["value"], f"[parameters('{name}')]")
        self.assertEqual(api["variables"]["sessionDeletionEnv"], [
            {
                "name": "AI4IA_SESSION_DELETION_ENABLED",
                "value": "[string(parameters('sessionDeletionEnabled'))]",
            },
            {
                "name": "AI4IA_SESSION_DELETION_ROLLOUT_ID",
                "value": "[parameters('sessionDeletionRolloutId')]",
            },
        ])
        self.assertIn("variables('sessionDeletionEnv')", json.dumps(api["variables"]["apiEnv"]))

    def test_hard_quota_rollout_defaults_off_and_reaches_api(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        for name, default in (("hardQuotaEnabled", False), ("hardQuotaRolloutId", "")):
            self.assertEqual(self.template["parameters"][name]["defaultValue"], default)
            self.assertEqual(api["parameters"][name]["defaultValue"], default)
            self.assertEqual(module["parameters"][name]["value"], f"[parameters('{name}')]")
        self.assertEqual(api["variables"]["hardQuotaEnv"], [
            {
                "name": "AI4IA_HARD_QUOTA_ENABLED",
                "value": "[string(parameters('hardQuotaEnabled'))]",
            },
            {
                "name": "AI4IA_HARD_QUOTA_ROLLOUT_ID",
                "value": "[parameters('hardQuotaRolloutId')]",
            },
        ])
        self.assertIn("variables('hardQuotaEnv')", json.dumps(api["variables"]["apiEnv"]))

    def test_webiq_limits_and_endpoint_reach_the_api_without_exposing_credentials(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        for name, default, maximum in (
            ("webSearchMaxResults", 5, 50),
            ("webSearchMaxContentChars", 6000, 500000),
        ):
            with self.subTest(parameter=name):
                self.assertEqual(self.template["parameters"][name]["defaultValue"], default)
                self.assertEqual(self.template["parameters"][name]["minValue"], 1)
                self.assertEqual(self.template["parameters"][name]["maxValue"], maximum)
                self.assertEqual(
                    module["parameters"][name]["value"], f"[parameters('{name}')]"
                )
                self.assertEqual(api["parameters"][name]["defaultValue"], default)
                self.assertEqual(api["parameters"][name]["maxValue"], maximum)
                self.assertIn(
                    f"string(parameters('{name}'))",
                    json.dumps(api["variables"]["webSearchEnv"]),
                )
        self.assertEqual(
            module["parameters"]["webIqBaseUrl"]["value"], "[parameters('webIqBaseUrl')]"
        )
        self.assertEqual(api["parameters"]["webIqApiKey"]["type"].lower(), "securestring")
        emitted = json.dumps(api["variables"]["webSearchEnv"])
        self.assertIn("AI4IA_WEB_SEARCH_MAX_RESULTS", emitted)
        self.assertIn("AI4IA_WEB_SEARCH_MAX_CONTENT_CHARS", emitted)
        self.assertIn("AI4IA_WEBIQ_BASE_URL", emitted)
        self.assertNotIn("value', parameters('webIqApiKey')", emitted)

    def test_media_gates_emit_both_boolean_values_independently_of_storage(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        expected = []
        for parameter, env_name in (
            ("imageGenerationEnabled", "AI4IA_IMAGE_GENERATION_ENABLED"),
            ("videoGenerationEnabled", "AI4IA_VIDEO_GENERATION_ENABLED"),
        ):
            self.assertFalse(self.template["parameters"][parameter]["defaultValue"])
            self.assertEqual(
                module["parameters"][parameter]["value"], f"[parameters('{parameter}')]"
            )
            self.assertFalse(api["parameters"][parameter]["defaultValue"])
            expected.append(
                {"name": env_name, "value": f"[string(parameters('{parameter}'))]"}
            )
        self.assertEqual(api["variables"].get("mediaFeatureEnv"), expected)
        self.assertIn("variables('mediaFeatureEnv')", api["variables"]["apiEnv"])

    def test_openapi_false_is_emitted_rather_than_treated_as_unset(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        self.assertEqual(
            module["parameters"]["apiOpenapiEnabled"]["value"],
            "[parameters('apiOpenapiEnabled')]",
        )
        self.assertEqual(
            api["variables"]["openapiEnv"],
            [
                {
                    "name": "AI4IA_OPENAPI_ENABLED",
                    "value": "[string(parameters('apiOpenapiEnabled'))]",
                }
            ],
        )
        self.assertIn("variables('openapiEnv')", api["variables"]["apiEnv"])

    def test_search_metrics_use_the_search_deployment_location(self) -> None:
        self.assertEqual(
            self.template["variables"].get("effectiveSearchLocation"),
            "[if(empty(parameters('searchLocation')), parameters('location'), "
            "parameters('searchLocation'))]",
        )
        search = self.template["resources"]["search"]["properties"]
        api = self.template["resources"]["api"]["properties"]
        self.assertEqual(
            search["parameters"]["location"]["value"],
            "[variables('effectiveSearchLocation')]",
        )
        self.assertEqual(
            api["parameters"]["metricsSearchLocation"]["value"],
            "[variables('effectiveSearchLocation')]",
        )
        emitted = api["template"]["variables"]["resourceMetricsEnv"]
        self.assertIn("AI4IA_METRICS_SEARCH_ENDPOINT", emitted)
        self.assertIn(
            "format('https://{0}.metrics.monitor.azure.com', "
            "if(empty(parameters('metricsSearchLocation')), parameters('location'), "
            "parameters('metricsSearchLocation')))",
            emitted,
        )

    def test_null_forgiving_access_preserves_conditional_durable_outputs(self) -> None:
        parameters = self.template["resources"]["api"]["properties"]["parameters"]
        self.assertEqual(
            parameters["durableTaskEndpoint"],
            "[if(parameters('enableDurableWorkflows'), createObject('value', "
            "reference('durabletask').outputs.endpoint.value), createObject('value', ''))]",
        )
        self.assertEqual(
            parameters["durableTaskHubName"],
            "[if(parameters('enableDurableWorkflows'), createObject('value', "
            "reference('durabletask').outputs.taskHubName.value), createObject('value', ''))]",
        )

    def test_endpoint_normalization_has_contracts_and_nonnegative_bounds(self) -> None:
        gateway = self.template["resources"]["gateway"]["properties"]["template"]
        for parameter in (
            "primaryFoundryEndpoint",
            "speechVoiceLiveAccountEndpoint",
        ):
            self.assertEqual(gateway["parameters"][parameter]["minLength"], 1)

        realtime = gateway["variables"]["primaryFoundryRealtimeWssUrl"]
        speech = gateway["variables"]["speechVoiceLiveAccountBase"]
        self.assertIn(
            "max(sub(length(parameters('primaryFoundryEndpoint')), 1), 0)",
            realtime,
        )
        self.assertIn(
            "max(sub(length(parameters('speechVoiceLiveAccountEndpoint')), 1), 0)",
            speech,
        )
        self.assertIn("'https://', 'wss://'", realtime)

    def test_primary_cu_outputs_use_the_same_records_sent_to_model_modules(self) -> None:
        module_parameters = self.template["resources"]["modelDeployments"]["properties"][
            "parameters"
        ]
        module_value = module_parameters["deployments"]["value"]
        self.assertEqual(
            module_value,
            "[variables('modelDeploymentsByRegion')[copyIndex()]]",
        )
        self.assertEqual(
            module_parameters["claudeOrganizationName"]["value"],
            "[parameters('claudeOrganizationName')]",
        )
        self.assertEqual(
            module_parameters["claudeCountryCode"]["value"],
            "[parameters('claudeCountryCode')]",
        )
        self.assertEqual(
            module_parameters["claudeIndustry"]["value"],
            "[parameters('claudeIndustry')]",
        )
        api_parameters = self.template["resources"]["api"]["properties"]["parameters"]
        self.assertEqual(
            api_parameters["claudeEnabled"]["value"],
            "[parameters('claudeEnabled')]",
        )
        variables = self.template["variables"]
        self.assertEqual(
            variables["primaryModelDeployments"],
            "[variables('modelDeploymentsByRegion')[variables('primaryFoundryIndex')]]",
        )
        self.assertIn("'gpt-5.2'", variables["primaryCuCompletionDeployment"])
        self.assertIn(
            "'text-embedding-3-large'",
            variables["primaryCuEmbeddingDeployment"],
        )
        outputs = self.template["outputs"]
        expected_models = outputs["AZURE_EXPECTED_MODEL_DEPLOYMENTS"]["copy"]["input"]
        self.assertIn(
            "variables('modelDeploymentsByRegion')",
            expected_models["deploymentNames"],
        )
        self.assertIn("deploymentName", expected_models["deploymentNames"])
        self.assertEqual(
            outputs["AZURE_PRIMARY_FOUNDRY_REGION"]["value"],
            "[parameters('location')]",
        )
        self.assertEqual(
            outputs["AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT"]["value"],
            "[variables('primaryCuCompletionDeployment').deploymentName]",
        )
        self.assertEqual(
            outputs["AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT"]["value"],
            "[variables('primaryCuEmbeddingDeployment').deploymentName]",
        )


class ProductionCapacityCompiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        shutil.copy2(ROOT / "infra" / "capacity.bicep", self.directory / "capacity.bicep")
        self.document = production_document()

    def build_parameters(
        self, document: dict, profiles: tuple[str, ...] = ("baseline", "maximum", "production"),
    ) -> subprocess.CompletedProcess[str]:
        (self.directory / "models.json").write_text(json.dumps(document), encoding="utf-8")
        source = """using none
import { selectedCapacity } from './capacity.bicep'
var models = loadJsonContent('models.json')
param selections = {
"""
        source += "\n".join(
            f"  {profile}: map(models.catalog[0].deployments, d => selectedCapacity(d, '{profile}'))"
            for profile in profiles
        ) + "\n}\n"
        parameters = self.directory / "fixture.bicepparam"
        parameters.write_text(source, encoding="utf-8")
        bicep, az = shutil.which("bicep"), shutil.which("az")
        if bicep:
            command = [bicep, "build-params", str(parameters), "--stdout"]
        elif az:
            command = [az, "bicep", "build-params", "--file", str(parameters), "--stdout"]
        else:
            self.fail("The same Bicep compiler used by infra-validate is required.")
        return subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)

    def test_real_bicep_evaluates_configured_profile_and_maximum_fallback(self) -> None:
        from scripts.tests.test_model_capacity_profile import preflight

        del self.document["catalog"][0]["deployments"][1]["maxCapacity"]
        result = self.build_parameters(self.document)
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        parameters = json.loads(envelope["parametersJson"]) if "parametersJson" in envelope else envelope
        actual = parameters["parameters"]["selections"]["value"]
        self.assertEqual(actual, {"baseline": [10, 10], "maximum": [100, 10], "production": [40, 20]})
        for profile, capacities in actual.items():
            required = preflight.catalog_requirements(self.document, capacity_profile=profile)
            self.assertEqual(capacities, [d["capacity"] for region in required.values() for d in region])

    def test_missing_production_metadata_is_a_real_bicep_error_not_zero_or_fallback(self) -> None:
        allowed = self.build_parameters(self.document)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        for missing in ("production", "capacity"):
            document = production_document()
            deployment = document["catalog"][0]["deployments"][0]
            if missing == "production":
                del deployment["production"]
            else:
                del deployment["production"]["capacity"]
            with self.subTest(missing=missing):
                unchanged = self.build_parameters(document, ("baseline", "maximum"))
                self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
                denied = self.build_parameters(document)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("Error", denied.stderr)

    def test_full_root_compiles_with_the_same_configured_catalog(self) -> None:
        infra = self.directory / "infra"
        shutil.copytree(ROOT / "infra", infra)
        document = production_document(include_cu=True)
        (infra / "models.json").write_text(json.dumps(document), encoding="utf-8")
        result = _compile(infra / "main.bicep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(result.stderr, r"\(\d+,\d+\)\s*:\s*(?:Warning|Error)")
        template = json.loads(result.stdout)
        self.assertIn(document, template["variables"].values())
        self.assertIn("production", template["parameters"]["modelCapacityProfile"]["allowedValues"])
        self.assertIn("__bicep.selectedCapacity", json.dumps(template["variables"]["copy"]))


if __name__ == "__main__":
    unittest.main()
