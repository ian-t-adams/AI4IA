"""Offline controls for the finite private recovery module inputs."""
from copy import deepcopy
import json
import unittest

from scripts import _private_recovery_packets as packets
from scripts.tests.test_bicep_compilation import ROOT, _compile

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
GROUP = "rg-ai4ia-test"
PREFIX = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
IDENTITY = PREFIX + "Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test"
IMAGE = "testregistry.azurecr.io/ai4ia/api-test@sha256:" + "a" * 64


def source():
    return {
        "id": PREFIX + "Microsoft.App/containerApps/ca-api-test",
        "tags": {"azd-service-name": "api"},
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {IDENTITY: {}}},
        "properties": {"configuration": {
            "registries": [{"server": "testregistry.azurecr.io", "identity": IDENTITY}],
            "secrets": [{"name": "model-gateway-api-key", "keyVaultUrl": None, "identity": None}],
        }},
    }


def overrides(app=None, phase="candidate", image=IMAGE):
    return packets.api_overrides(
        phase=phase, subscription=SUBSCRIPTION, group=GROUP, workload="ai4ia",
        environment="test", source=source() if app is None else app, image=image,
    )


class RecoveryInputTests(unittest.TestCase):
    def test_null_secrets_match_empty_without_discarding_nonempty_or_invalid_metadata(self):
        empty = {"ingress": {"external": True}, "secrets": []}
        null = {"ingress": {"external": True}, "secrets": None}
        self.assertEqual(packets.configuration_intent(null), packets.configuration_intent(empty))
        self.assertIsNone(null["secrets"])
        self.assertNotEqual(packets.configuration_intent({
            "ingress": {"external": True}, "secrets": [{"name": "retained"}],
        }), packets.configuration_intent(empty))
        for value in (False, 0, {}, "", [None]):
            with self.subTest(value=value), self.assertRaises(packets.PacketError):
                packets.configuration_intent({"ingress": {"external": True}, "secrets": value})

    def test_location_name_and_display_name_require_one_exact_arm_identity(self):
        row = {"id": f"/subscriptions/{SUBSCRIPTION}/locations/eastus2",
               "name": "eastus2", "displayName": "East US 2", "type": "Region"}
        evidence = packets.location_evidence({"rows": [row], "nextLink": None}, SUBSCRIPTION, "eastus2")
        for value in ("eastus2", "East US 2", "EASTUS2", "east us 2"):
            self.assertEqual(packets.canonical_location(value, evidence), "eastus2")
        for value in (None, "", True, {}, "westus", "West US", "East-US-2", "East  US 2",
                      " East US 2", "eastus2 ", "(US) East US 2", "global"):
            with self.subTest(value=value), self.assertRaises(packets.PacketError):
                packets.canonical_location(value, evidence)
        for data in (
            {"rows": []}, {"rows": [row], "nextLink": "more"}, {"rows": [row], "nextLink": False},
            {"rows": [row, row]}, {"rows": [row, {**row, "name": "other"}]},
            {"rows": [{**row, "id": row["id"].replace(SUBSCRIPTION, "other")}]},
            {"rows": [{**row, "type": "EdgeZone"}]}, {"rows": [{**row, "displayName": None}]},
            {"rows": [{**row, "name": "westus"}]},
        ):
            with self.subTest(data=data), self.assertRaises(packets.PacketError):
                packets.location_evidence(data, SUBSCRIPTION, "eastus2")

    def test_exact_source_target_and_existing_identity(self):
        result = overrides()
        self.assertEqual(result["appName"], "ca-api-private-test")
        self.assertEqual(result["apiIdentityResourceId"], IDENTITY)
        self.assertEqual(result["recoverySource"], "legacy")
        self.assertIs(result["recoveryCandidate"], True)
        self.assertIs(result["privateCosmosRequired"], True)
        self.assertEqual(result["recoverySecretDescriptors"], [{"name": "model-gateway-api-key"}])
        self.assertEqual(result["apiImage"], IMAGE)
        app = source()
        app["identity"]["userAssignedIdentities"] = {IDENTITY.lower(): {}}
        self.assertEqual(overrides(app), result)
        app["id"] = PREFIX + "Microsoft.App/containerApps/ca-api-private-test"
        activated = overrides(app, phase="activate")
        self.assertEqual(activated["recoverySource"], "private")
        self.assertIs(activated["recoveryCandidate"], False)

    def test_wrong_source_identity_registry_or_image_refuses(self):
        mutations = [
            lambda app: app.update(id=PREFIX + "Microsoft.App/containerApps/another"),
            lambda app: app["identity"].update(type="SystemAssigned"),
            lambda app: app["identity"]["userAssignedIdentities"].update({IDENTITY + "-other": {}}),
            lambda app: app["properties"]["configuration"]["registries"][0].update(identity=IDENTITY + "-other"),
        ]
        for mutate in mutations:
            app = source()
            mutate(app)
            with self.subTest(mutate=mutate), self.assertRaises(packets.PacketError):
                overrides(app)
        for image in (IMAGE.replace("testregistry", "otherregistry"), IMAGE.replace("/api-", "/web-"),
                      IMAGE.split("@")[0] + ":latest", "mcr.microsoft.com/k8se/quickstart:latest"):
            with self.subTest(image=image), self.assertRaises(packets.PacketError):
                overrides(image=image)
        with self.assertRaises(packets.PacketError):
            overrides(phase="private")

    def test_key_vault_reference_is_preserved_not_resolved(self):
        row = {"name": "webiq-api-key", "keyVaultUrl": "https://test-vault.vault.azure.net/secrets/webiq",
               "identity": IDENTITY.lower()}
        self.assertEqual(packets.secret_descriptors([row], IDENTITY), [row])
        self.assertNotIn("value", packets.secret_descriptors([row], IDENTITY)[0])
        for url in (row["keyVaultUrl"] + "?sig=forbidden", "https://outside.example/secrets/key"):
            with self.assertRaises(packets.PacketError):
                packets.secret_descriptors([{**row, "keyVaultUrl": url}], IDENTITY)
        with self.assertRaises(packets.PacketError):
            packets.secret_descriptors([{**row, "identity": "system"}], IDENTITY)

    def test_secret_values_unknown_names_and_duplicates_fail_without_echo(self):
        marker = "must-not-escape-super-secret-value"
        row = {"name": "model-gateway-api-key"}
        for rows in ([{**row, "value": marker}], [{**row, "value": None}],
                     [{"name": marker}], [row, row], None,
                     [{**row, "identity": IDENTITY}]):
            with self.subTest(rows=rows), self.assertRaises(packets.PacketError) as caught:
                packets.secret_descriptors(rows, IDENTITY)
            self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(packets.secret_descriptors([row], IDENTITY), [row])

    def test_record_projection_never_requests_secure_values(self):
        template = {"parameters": {
            "location": {"type": "string"},
            "modelGatewayApiKey": {"type": "securestring", "defaultValue": ""},
            "recoverySource": {"type": "string", "defaultValue": "none"},
        }}
        metadata = {"names": ["location", "modelGatewayApiKey"], "types": ["String", "SecureString"],
                    "timestamp": "2026-10-24T00:00:00Z", "hash": "1234", "state": "Succeeded"}
        calls = []

        def read(args):
            calls.append(args)
            return {"location": "eastus2"} if args[-1].startswith("properties.parameters.") else metadata

        observed, values = packets.read_record(read, template, SUBSCRIPTION, GROUP, "api")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], calls[2])
        self.assertIn("location:location.value", calls[1][-1])
        self.assertNotIn("modelGatewayApiKey", calls[1][-1])
        result = packets.record_parameters(template, observed, values, {"recoverySource": "legacy"})
        self.assertEqual(result, {"location": {"value": "eastus2"}, "recoverySource": {"value": "legacy"}})
        with self.assertRaises(packets.PacketError):
            packets.record_parameters(template, observed, {**values, "modelGatewayApiKey": "secret"}, {})
        with self.assertRaises(packets.PacketError):
            packets.record_parameters(template, observed, values, {"modelGatewayApiKey": "secret"})
        with self.assertRaises(packets.PacketError):
            packets.record_parameters(template, {**metadata, "types": ["String", "String"]}, values, {})
        count = 0

        def changing(args):
            nonlocal count
            count += 1
            return {**metadata, "hash": "changed"} if count == 3 else read(args)

        with self.assertRaises(packets.PacketError):
            packets.read_record(changing, template, SUBSCRIPTION, GROUP, "api")

    def test_only_approved_exemption_is_removed_from_copied_tags(self):
        template = {"parameters": {"tags": {"type": "object"}}}
        meta = {"names": ["tags"], "types": ["Object"], "timestamp": "time", "hash": "123", "state": "Succeeded"}
        original = {"tags": {"SecurityControl": "Ignore", "ordinary": "retained"}}
        result = packets.record_parameters(template, meta, original, {})
        self.assertEqual(result["tags"]["value"], {"ordinary": "retained"})
        self.assertEqual(original["tags"]["SecurityControl"], "Ignore")
        with self.assertRaises(packets.PacketError):
            packets.record_parameters(template, meta, {"tags": {"SecurityControl": "different"}}, {})

    def test_new_environment_quota_requires_actual_finite_samples(self):
        data = {"value": [{"name": {"value": "ManagedEnvironmentConsumptionCores"},
                           "currentValue": 0, "usage": 0, "limit": 0.5}]}
        self.assertEqual(packets.consumption_headroom(data, 0.5)["requiredAdditionalCores"], 0.5)
        for mutation in (
            lambda item: item.update(nextLink="more"),
            lambda item: item["value"][0].update(currentValue=None),
            lambda item: item["value"][0].update(currentValue=True),
            lambda item: item["value"][0].update(currentValue=0.1, usage=0.1),
            lambda item: item["value"][0].update(limit=float("inf")),
            lambda item: item["value"].append(deepcopy(item["value"][0])),
            lambda item: item["value"][0]["name"].update(value="ManagedEnvironmentGeneralPurposeCores"),
        ):
            copy = deepcopy(data)
            mutation(copy)
            with self.assertRaises(packets.PacketError):
                packets.consumption_headroom(copy, 0.5)

    def test_only_masked_secret_values_can_enter_configuration_comparison(self):
        before = {"activeRevisionsMode": "Single", "ingress": {"external": True},
                  "secrets": [{"name": "model-gateway-api-key"}]}
        masked = deepcopy(before)
        masked["secrets"][0]["value"] = "********"
        self.assertEqual(packets.configuration_intent(before), packets.configuration_intent(masked))
        for value in ("sensitive-value-never-print", "[listSecrets('unknown')]", "", 123):
            copy = deepcopy(before)
            copy["secrets"][0]["value"] = value
            with self.assertRaises(packets.PacketError) as caught:
                packets.configuration_intent(copy)
            self.assertNotIn(str(value), str(caught.exception)) if value else None
        changed = deepcopy(before)
        changed["ingress"]["ipSecurityRestrictions"] = [{"name": "retain-the-acl"}]
        self.assertNotEqual(packets.configuration_intent(before), packets.configuration_intent(changed))


class RecoveryCompiledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = {}
        for name in ("api", "web"):
            result = _compile(ROOT / "infra" / "modules" / f"{name}.bicep")
            if result.returncode or result.stderr.strip():
                raise AssertionError(f"{name} compiler diagnostics: {result.stderr}")
            cls.modules[name] = json.loads(result.stdout)

    def assert_api_boundary(self, api):
        self.assertEqual(api["parameters"]["recoverySource"]["defaultValue"], "none")
        self.assertEqual(api["parameters"]["recoverySource"]["allowedValues"], ["none", "legacy", "private"])
        self.assertEqual(api["parameters"]["recoverySecretDescriptors"]["defaultValue"], [])
        self.assertEqual(api["parameters"]["recoverySecretDescriptors"]["maxLength"], 8)
        descriptor = api["definitions"]["RecoverySecretDescriptor"]
        self.assertIs(descriptor["additionalProperties"], False)
        self.assertEqual(set(descriptor["properties"]), {"name", "keyVaultUrl", "identity"})
        self.assertEqual(set(descriptor["properties"]["name"]["allowedValues"]), packets.SECRET_NAMES)
        self.assertEqual(api["variables"]["inheritsSecrets"], "[not(equals(parameters('recoverySource'), 'none'))]")
        resources = api["resources"]
        self.assertIs(resources["secretSource"]["existing"], True)
        self.assertIn("format('ca-api-{0}', parameters('environmentName'))", resources["secretSource"]["name"])
        self.assertIn("format('ca-api-private-{0}', parameters('environmentName'))", resources["secretSource"]["name"])
        inherited = resources["apiApp"]["properties"]["configuration"]["secrets"]
        self.assertTrue(inherited.startswith("[if(variables('inheritsSecrets'), map("))
        self.assertIn("listSecrets('secretSource', '2025-07-01')", inherited)
        self.assertIn("equals(lambdaVariables('current').name, lambdaVariables('secret').name)", inherited)
        self.assertIn("'keyVaultUrl', tryGet(lambdaVariables('secret'), 'keyVaultUrl')", inherited)
        self.assertIn("'identity', tryGet(lambdaVariables('secret'), 'identity')", inherited)
        self.assertNotIn("listSecrets", json.dumps(api["outputs"]))
        app = resources["apiApp"]
        self.assertEqual(app["name"], "[variables('apiAppName')]")
        self.assertIn("if(variables('inheritsSecrets'), format('ca-api-private-{0}'", api["variables"]["apiAppName"])
        self.assertIn("resourceId('Microsoft.ManagedIdentity/userAssignedIdentities'", api["variables"]["effectiveIdentityId"])
        self.assertEqual(app["identity"]["userAssignedIdentities"], {"[format('{0}', variables('effectiveIdentityId'))]": {}})
        self.assertEqual(resources["apiDiagnostics"]["condition"], "[not(variables('inheritsSecrets'))]")
        self.assertEqual({row["type"] for row in resources.values() if not row.get("existing")},
                         {"Microsoft.App/containerApps", "Microsoft.Insights/diagnosticSettings"})

    def test_api_compilation_retains_exact_secret_and_resource_boundary(self):
        self.assert_api_boundary(self.modules["api"])
        variables = self.modules["api"]["variables"]
        self.assertEqual(variables["apiAppResourceId"],
                         "[resourceId('Microsoft.App/containerApps', variables('apiAppName'))]")
        self.assertIn("'name', 'AI4IA_METRICS_CONTAINER_APP_RESOURCE_ID', 'value', variables('apiAppResourceId')",
                      variables["resourceMetricsEnv"])

    def test_compiled_speech_key_keeps_both_feature_gates_and_default_off_inheritance(self):
        variables = self.modules["api"]["variables"]
        self.assertEqual(
            variables["hasSpeechVoiceLiveGatewayKey"],
            "[and(and(parameters('realtimeEnabled'), parameters('speechVoiceLiveEnabled')), "
            "or(not(empty(parameters('speechVoiceLiveGatewayApiKey'))), "
            "contains(variables('inheritedSecretNames'), 'speech-voice-live-gateway-api-key')))]",
        )
        self.assertEqual(
            variables["inheritedSecretNames"],
            "[if(variables('inheritsSecrets'), map(parameters('recoverySecretDescriptors'), "
            "lambda('secret', lambdaVariables('secret').name)), createArray())]",
        )
        self.assertEqual(variables["inheritsSecrets"], "[not(equals(parameters('recoverySource'), 'none'))]")

    def test_boundary_assertions_reject_removed_guards(self):
        for mutate in (
            lambda data: data["variables"].update(inheritsSecrets="[true()]"),
            lambda data: data["resources"]["apiDiagnostics"].update(condition="[true()]"),
            lambda data: data["definitions"]["RecoverySecretDescriptor"].update(additionalProperties=True),
            lambda data: data["resources"]["apiApp"]["identity"].update(userAssignedIdentities={"other": {}}),
        ):
            mutant = deepcopy(self.modules["api"])
            mutate(mutant)
            with self.subTest(mutate=mutate), self.assertRaises(AssertionError):
                self.assert_api_boundary(mutant)

    def test_web_cutover_keeps_existing_certificate_and_no_diagnostic_write(self):
        web = self.modules["web"]
        self.assertIs(web["parameters"]["recoveryCutover"]["defaultValue"], False)
        resources = web["resources"]
        if isinstance(resources, list):
            resources = {row["type"].split("/")[-1]: row for row in resources}
            cert, diagnostic = resources["managedCertificates"], resources["diagnosticSettings"]
        else:
            cert, diagnostic = resources["webCert"], resources["webDiagnostics"]
        self.assertIn("not(parameters('recoveryCutover'))", cert["condition"])
        self.assertEqual(diagnostic["condition"], "[not(parameters('recoveryCutover'))]")
        self.assertIn("resourceId('Microsoft.App/managedEnvironments/managedCertificates'",
                      web["variables"]["webCustomDomains"])


if __name__ == "__main__":
    unittest.main()
