"""Offline full-phase preview, binding, and no-secret-output controls."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import jmespath

from scripts._api_private import deployment_operation
from scripts.tests._loader import load_script
from scripts.tests.test_api_private_network import Cloud, ENV, MODULE
from scripts.tests.test_bicep_compilation import _compile

ROOT = Path(__file__).resolve().parents[2]
with patch("sys.path", [str(ROOT / "scripts"), *sys.path]):
    PREVIEW = load_script("private_preview", ROOT / "scripts/preview-api-private.py", register=True)


def fixture():
    bound = {
        "source": "a" * 40, "phase": "candidate", "subscription": ENV["AZURE_SUBSCRIPTION_ID"],
        "environment": "test", "workload": "ai4ia", "location": "eastus2",
        "resourceGroup": "rg-ai4ia-test", "template": "b" * 64, "parameters": "c" * 64,
        "cosmosAccount": "cosmos-test",
    }
    prefix = f"/subscriptions/{bound['subscription']}/resourceGroups/rg-ai4ia-test/providers/"
    vnet = prefix + "Microsoft.Network/virtualNetworks/vnet-ai4ia-test-api-private"
    endpoint = prefix + "Microsoft.Network/privateEndpoints/pe-ai4ia-test-api-private-cosmos"
    zone = prefix + "Microsoft.Network/privateDnsZones/privatelink.documents.azure.com"
    ids = [
        vnet, endpoint, endpoint + "/privateDnsZoneGroups/default", zone,
        zone + "/virtualNetworkLinks/ai4ia-test-api-private",
        prefix + "Microsoft.App/managedEnvironments/cae-ai4ia-test-api-private",
        prefix + "Microsoft.App/containerApps/ca-api-private-test",
    ]
    raw = {"status": "Succeeded", "changes": [
        {"resourceId": rid, "changeType": "NoChange"} for rid in ids
    ]}
    params = {
        "apiPrivateVnetPrefix": {"value": "10.241.0.0/25"},
        "apiPrivateSubnetPrefix": {"value": "10.241.0.0/26"},
        "cosmosPrivateEndpointSubnetPrefix": {"value": "10.241.0.64/27"},
    }
    return bound, raw, params


class PreviewTests(unittest.TestCase):
    def test_foundation_context_uses_actual_direct_module_parameter_contract(self):
        api = json.loads(_compile(ROOT / "infra/modules/api.bicep").stdout)
        network = json.loads(_compile(ROOT / "infra/modules/api-private-network.bicep").stdout)
        prefix = f"/subscriptions/{ENV['AZURE_SUBSCRIPTION_ID']}/resourceGroups/rg-ai4ia-test/providers/"
        defaults = {"string": "", "securestring": "secret-must-not-escape", "bool": False,
                    "int": 0, "object": {}, "array": []}
        values = {name: deepcopy(row.get("defaultValue", defaults[row["type"]]))
                  for name, row in api["parameters"].items()}
        values.update(environmentName="test", appEnvironment="prod", authProvider="entra",
                      entraTenantId="tenant", entraAudience="audience", location="eastus2", tags={"owner": "test"},
                      logAnalyticsWorkspaceId=prefix + "Microsoft.OperationalInsights/workspaces/log-ai4ia-test")
        deployment = {"properties": {
            "timestamp": "2026-10-24T00:00:00Z", "templateHash": "12345", "provisioningState": "Succeeded",
            "parameters": {name: {"type": api["parameters"][name]["type"], "value": value}
                           for name, value in values.items()},
        }}
        app = {
            "id": prefix + "Microsoft.App/containerApps/ca-api-test", "location": "eastus2",
            "tags": {"owner": "test"}, "identity": {}, "properties": {
                "managedEnvironmentId": prefix + "Microsoft.App/managedEnvironments/old",
                "latestReadyRevisionName": "ca-api-test--ready", "latestRevisionName": "ca-api-test--ready",
                "configuration": {"secrets": []},
                "template": {"containers": [{"name": "api",
                    "image": "example.azurecr.io/ai4ia/api-test@sha256:" + "a" * 64,
                    "env": [{"name": "AI4IA_COSMOS_ENDPOINT", "value": "https://cosmos-test.documents.azure.com:443/"}],
                }], "scale": {"minReplicas": 1, "maxReplicas": 3}},
            },
        }
        env = {**ENV, "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40,
               "AZURE_TENANT_ID": "tenant", "AI4IA_PRIVATE_STEP": "foundation",
               "AI4IA_PRIVATE_COSMOS_ENDPOINT": "https://cosmos-test.documents.azure.com:443/"}
        calls = []
        def read(args):
            calls.append(args)
            source = deployment if args[:3] == ["deployment", "group", "show"] else app
            if args[:3] == ["containerapp", "revision", "show"]:
                return app["properties"]["template"]
            return jmespath.search(args[args.index("--query") + 1], source)
        with patch.object(PREVIEW, "compile_module", side_effect=lambda name: {
            "api.bicep": api, "api-private-network.bicep": network,
        }[name]) as compiled:
            template, params, bound, baseline, expected = PREVIEW.compiled_context(env, read)
        self.assertEqual(template, network)
        self.assertEqual(set(params), set(network["parameters"]))
        self.assertEqual(expected["apiAuthProvider"], {"value": "entra"})
        self.assertEqual(bound["packet"], "foundation")
        self.assertIsNone(baseline)
        self.assertEqual([call.args[0] for call in compiled.call_args_list],
                         ["api.bicep", "api-private-network.bicep"])
        self.assertNotIn("secret-must-not-escape", json.dumps((params, bound, expected)))
        projected = next(args[-1] for args in calls if args[-1].startswith("properties.parameters."))
        for name, definition in api["parameters"].items():
            if definition["type"] == "securestring":
                self.assertNotIn(name + ".value", projected)

    def test_foundation_is_exactly_its_six_resources_not_the_root(self):
        bound, raw, params = fixture()
        bound["packet"] = "foundation"
        raw["changes"].pop()
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        extra = {"resourceId": raw["changes"][0]["resourceId"].split("/providers/")[0]
                 + "/providers/Microsoft.ApiManagement/service/unrelated",
                 "changeType": "Modify", "before": {"tags": {"SecurityControl": "Ignore"}},
                 "after": {"tags": {}}, "delta": [{"path": "tags.SecurityControl"}]}
        self.assertIn("change_outside_packet",
                      PREVIEW.scope_preview({**raw, "changes": [*raw["changes"], extra]}, bound, params)["blockingReasons"])
        raw["changes"][0]["changeType"] = "Ignore"
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.scope_preview(raw, bound, params)

    def test_candidate_create_preserves_current_app_and_rejects_unprotected_values(self):
        bound, _, params = fixture()
        bound["packet"] = "candidate"
        prefix = f"/subscriptions/{bound['subscription']}/resourceGroups/{bound['resourceGroup']}/providers/"
        identity = prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test"
        legacy = prefix + "Microsoft.App/containerApps/ca-api-test"
        target = prefix + "Microsoft.App/containerApps/ca-api-private-test"
        image = "example.azurecr.io/ai4ia/api-test@sha256:" + "a" * 64
        params.update(apiPrivateInitialImage={"value": image}, entraTenantId={"value": "tenant"},
                      entraAudience={"value": "audience"})
        baseline = {"id": legacy, "location": "eastus2", "tags": {"azd-service-name": "api"},
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity: {}}},
            "properties": {"managedEnvironmentId": prefix + "Microsoft.App/managedEnvironments/old",
                "configuration": {"activeRevisionsMode": "Single", "secrets": [{"name": "model-gateway-api-key"}],
                    "ingress": {"external": True, "targetPort": 8080, "transport": "auto", "allowInsecure": False},
                    "registries": [{"server": "example.azurecr.io", "identity": identity}]},
                "template": {"scale": {"minReplicas": 1, "maxReplicas": 3}, "containers": [{
                    "name": "api", "image": image.replace("a" * 64, "b" * 64),
                    "resources": {"cpu": 0.5, "memory": "1Gi"}, "env": [
                        {"name": key, "value": value} for key, value in (
                            ("AI4IA_ENV", "prod"), ("AI4IA_AUTH_PROVIDER", "entra"),
                            ("AI4IA_ENTRA_TENANT_ID", "tenant"), ("AI4IA_ENTRA_AUDIENCE", "audience"),
                            ("AI4IA_COSMOS_ENDPOINT", "https://cosmos-test.documents.azure.com:443/"),
                            ("OTHER_SETTING", "preserve"),
                            ("AI4IA_METRICS_CONTAINER_APP_RESOURCE_ID", legacy),
                            ("AI4IA_METRICS_COSMOS_RESOURCE_ID", prefix + "Microsoft.DocumentDB/databaseAccounts/cosmos-test"),
                            ("AI4IA_METRICS_SEARCH_RESOURCE_ID", prefix + "Microsoft.Search/searchServices/search-test"),
                        )
                    ],
                }]}}}
        after = deepcopy(baseline)
        after["tags"]["azd-service-name"] = "api-private-candidate"
        props = after["properties"]
        props["managedEnvironmentId"] = prefix + "Microsoft.App/managedEnvironments/cae-ai4ia-test-api-private"
        props["template"]["scale"]["maxReplicas"] = 1
        props["template"]["containers"][0]["image"] = image
        PREVIEW.env_rows(props["template"])["AI4IA_METRICS_CONTAINER_APP_RESOURCE_ID"]["value"] = target
        props["template"]["containers"][0]["env"] += [
            {"name": "AI4IA_PRIVATE_RECOVERY_CANDIDATE", "value": "true"},
            {"name": "AI4IA_PRIVATE_COSMOS_REQUIRED", "value": "true"},
        ]
        props["configuration"]["secrets"][0]["value"] = "********"
        raw = {"status": "Succeeded", "changes": [{"resourceId": target, "changeType": "Create", "after": after}]}
        result = PREVIEW.scope_preview(raw, bound, params, baseline)
        self.assertEqual(result["blockingReasons"], [])
        self.assertIn("properties.configuration.secrets[].value", result["changes"][0]["fields"])
        self.assertNotIn("********", json.dumps(result))
        metric = "AI4IA_METRICS_CONTAINER_APP_RESOURCE_ID"
        for side, wrong in (
            ("source", target), ("source", legacy + "-stale"),
            ("source", legacy.replace("rg-ai4ia-test", "rg-other")),
            ("source", legacy.replace(bound["subscription"], "0" * 36)),
            ("target", legacy), ("target", target + "-other"),
            ("target", target.replace("rg-ai4ia-test", "rg-other")),
            ("target", target.replace(bound["subscription"], "0" * 36)),
            ("source", None), ("target", None), ("target", True),
        ):
            bad_source, bad_raw = deepcopy(baseline), deepcopy(raw)
            bad_template = (bad_source["properties"]["template"] if side == "source"
                            else bad_raw["changes"][0]["after"]["properties"]["template"])
            rows = PREVIEW.env_rows(bad_template)
            if wrong is None:
                bad_template["containers"][0]["env"].remove(rows[metric])
            else:
                rows[metric]["value"] = wrong
            with self.subTest(side=side, wrong=wrong):
                self.assertIn("candidate_metrics_self_id_invalid",
                              PREVIEW.scope_preview(bad_raw, bound, params, bad_source)["blockingReasons"])
        for name in ("AI4IA_METRICS_COSMOS_RESOURCE_ID", "AI4IA_METRICS_SEARCH_RESOURCE_ID"):
            changed = deepcopy(raw)
            PREVIEW.env_rows(changed["changes"][0]["after"]["properties"]["template"])[name]["value"] += "-other"
            self.assertIn("candidate_configuration_changed",
                          PREVIEW.scope_preview(changed, bound, params, baseline)["blockingReasons"])
        stale_source = deepcopy(baseline)
        stale_source["id"] = legacy + "-other"
        self.assertIn("candidate_metrics_self_id_invalid",
                      PREVIEW.scope_preview(raw, bound, params, stale_source)["blockingReasons"])
        casing_source, casing_raw = deepcopy(baseline), deepcopy(raw)
        casing_source["id"] = legacy.upper()
        PREVIEW.env_rows(casing_source["properties"]["template"])[metric]["value"] = legacy.upper()
        PREVIEW.env_rows(casing_raw["changes"][0]["after"]["properties"]["template"])[metric]["value"] = target.upper()
        self.assertEqual(PREVIEW.scope_preview(casing_raw, bound, params, casing_source)["blockingReasons"], [])
        changed = deepcopy(raw)
        changed["changes"][0]["after"]["properties"]["template"]["containers"][0]["env"][5]["value"] = "changed"
        self.assertIn("candidate_configuration_changed", PREVIEW.scope_preview(changed, bound, params, baseline)["blockingReasons"])
        changed = deepcopy(raw)
        changed["changes"][0]["after"]["properties"]["configuration"]["secrets"][0]["value"] = "unprotected-private-value"
        with self.assertRaises(ValueError) as caught:
            PREVIEW.scope_preview(changed, bound, params, baseline)
        self.assertNotIn("unprotected-private-value", str(caught.exception))

    def test_actual_cli_projection_excludes_values_and_keeps_reference_metadata(self):
        app = {
            "id": "synthetic", "properties": {
                "configuration": {"activeRevisionsMode": "Single", "secrets": [
                    {"name": "model-gateway-api-key", "value": "must-not-escape"},
                    {"name": "webiq-api-key", "value": "must-not-escape-either",
                     "keyVaultUrl": "https://example.vault.azure.net/secrets/webiq", "identity": "same-identity"},
                ]}, "template": {"containers": [{"name": "api", "env": [
                    {"name": "KEY", "secretRef": "model-gateway-api-key"},
                ]}]},
            },
        }
        projected = jmespath.search(PREVIEW.APP_SNAPSHOT, app)
        self.assertNotIn("must-not-escape", json.dumps(projected))
        self.assertEqual(projected["properties"]["configuration"]["secrets"][1], {
            "name": "webiq-api-key", "keyVaultUrl": "https://example.vault.azure.net/secrets/webiq",
            "identity": "same-identity",
        })
        self.assertEqual(projected["properties"]["template"], app["properties"]["template"])

    def test_scoped_binding_cannot_authorize_root_provisioning(self):
        env = {**ENV, "AI4IA_RELEASE_KIND": "deploy", "AI4IA_PRIVATE_PREVIEW_BINDING": "previously-reviewed"}
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "verify-binding"]), \
             patch.object(PREVIEW, "compiled_context", side_effect=AssertionError("root cannot consume a packet")), \
             patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(PREVIEW.main(), 1)

    def test_exact_frozen_packet_applies_once_and_refuses_claim_drift(self):
        for fault in ("none", "parameters", "main", "claimed", "unknown-claim", "admission"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                packet = root / "ai4ia-private-packet-123-test"
                packet.mkdir()
                output = root / "ai4ia-private-preview"
                output.mkdir()
                template = {"resources": []}
                parameters = {"privateSetting": {"value": "private-value-never-publish"}}
                bound, raw, expected = fixture()
                bound.update(packet="foundation", template=PREVIEW.digest(template),
                             parameters=PREVIEW.digest(parameters))
                preview = PREVIEW.scope_preview(raw, bound, expected)
                token = PREVIEW.make_token(bound, preview, int(PREVIEW.time.time()))
                proof = output / "proof.json"
                proof.write_text(json.dumps({"context": bound, "preview": preview}))
                (packet / "template.json").write_text(json.dumps(template))
                (packet / "parameters.json").write_text(json.dumps({"parameters": parameters}))
                if fault == "parameters":
                    (packet / "parameters.json").write_text('{"parameters":{}}')
                env = {**ENV, "RUNNER_TEMP": directory, "GITHUB_RUN_ID": "123", "GITHUB_SHA": bound["source"],
                       "GITHUB_REPOSITORY": "ian-t-adams/AI4IA", "AI4IA_RELEASE_KIND": "deploy",
                       "AI4IA_RELEASE_PROVISION": "true", "AI4IA_PRIVATE_PREVIEW_BINDING": token,
                       "AI4IA_PRIVATE_PACKET_DIR": str(packet), "AI4IA_PRIVATE_PREVIEW_PROOF": str(proof)}
                writes = []
                def command(args, **kwargs):
                    if args[0] == "gh":
                        return json.dumps({"object": {"sha": "f" * 40 if fault == "main" else bound["source"]}}).encode()
                    self.assertEqual(args[:4], ["az", "deployment", "group", "create"])
                    self.assertEqual(args[args.index("--template-file") + 1], str(packet / "template.json"))
                    self.assertEqual(args[args.index("--parameters") + 1], "@" + str(packet / "parameters.json"))
                    self.assertEqual(json.loads((packet / "parameters.json").read_text())["parameters"], parameters)
                    self.assertNotIn("sub", args)
                    self.assertNotIn("azd", args)
                    writes.append(args)
                    return b""
                claim = SimpleNamespace(returncode=0 if fault == "claimed" else 1, stdout=b"",
                    stderr=b"sensitive-provider-message" if fault == "unknown-claim" else b"ERROR: (DeploymentNotFound) absent")
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "apply"]), \
                     patch.object(PREVIEW, "compiled_context", return_value=(template, parameters, bound, None, expected)), \
                     patch.object(PREVIEW, "run", side_effect=command), \
                     patch.object(PREVIEW.subprocess, "run", return_value=claim), \
                     patch.object(PREVIEW.ADMISSION, "admission",
                                  side_effect=ValueError("blocked") if fault == "admission" else None), \
                     redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
                    self.assertEqual(PREVIEW.main(), 0 if fault == "none" else 1)
                self.assertEqual(len(writes), 1 if fault == "none" else 0)
                evidence = (output / "execution.json").read_text() if (output / "execution.json").exists() else ""
                self.assertNotIn("private-value-never-publish", evidence + stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("sensitive-provider-message", stderr.getvalue())
                if fault == "none":
                    self.assertEqual(json.loads(evidence)["runtimeAcceptance"], "not_yet_observed")
                self.assertFalse((packet / "parameters.json").exists())

    def test_binding_is_same_head_phase_scope_inputs_delta_and_fresh(self):
        bound, raw, params = fixture()
        result = PREVIEW.scope_preview(raw, bound, params)
        token = PREVIEW.make_token(bound, result, 1792860000)
        PREVIEW.verify_token(token, bound, result, 1792860010)
        for field in ("source", "phase", "subscription", "resourceGroup", "parameters", "template"):
            with self.subTest(field=field):
                changed = {**bound, field: "changed"}
                with self.assertRaises(PREVIEW.PreviewError):
                    PREVIEW.verify_token(token, changed, result, 1792860010)
        for now in (1792859999, 1792861801):
            with self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.verify_token(token, bound, result, now)
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.verify_token(token + "0", bound, result, 1792860010)
        altered = {**result, "rawChangeDigest": "d" * 64}
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.verify_token(token, bound, altered, 1792860010)

    def test_complete_nochange_is_not_missing_coverage_or_permission(self):
        bound, raw, params = fixture()
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        for change in (
            lambda data: data.update(status="Failed"),
            lambda data: data.update(error={"code": "AuthorizationFailed"}),
            lambda data: data.update(changes=[]),
            lambda data: data["changes"].pop(),
            lambda data: data.update(diagnostics=[{"code": "ExpansionLimit"}]),
        ):
            data = deepcopy(raw)
            change(data)
            with self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.scope_preview(data, bound, params)

    def test_delete_role_create_and_protected_cosmos_changes_refuse(self):
        bound, raw, params = fixture()
        for delta in (
            {"resourceId": raw["changes"][0]["resourceId"], "changeType": "Delete"},
            {"resourceId": raw["changes"][0]["resourceId"], "changeType": "Unsupported"},
            {"resourceId": raw["changes"][0]["resourceId"], "changeType": "Deploy"},
        ):
            data = deepcopy(raw)
            data["changes"][0] = delta
            self.assertTrue(PREVIEW.scope_preview(data, bound, params)["blockingReasons"])
        data = deepcopy(raw)
        data["changes"].append({
            "resourceId": f"/subscriptions/{bound['subscription']}/resourceGroups/rg-ai4ia-test/providers/Microsoft.Authorization/roleAssignments/unapproved",
            "changeType": "Create", "after": {"location": "eastus2"},
        })
        result = PREVIEW.scope_preview(data, bound, params)
        self.assertIn("unapproved_create", result["blockingReasons"])
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.verify_token(PREVIEW.make_token(bound, result, 1792860000), bound, result, 1792860001)

    def test_network_addresses_delegation_and_no_exemption_are_actual_values(self):
        bound, raw, params = fixture()
        raw["changes"][0].update(changeType="Create", after={"location": "eastus2", "tags": {},
            "properties": {"addressSpace": {"addressPrefixes": ["10.241.0.0/25"]},
                           "dhcpOptions": {"dnsServers": []}, "subnets": [
                {"name": "api", "properties": {"addressPrefix": "10.241.0.0/26", "delegations": [
                    {"properties": {"serviceName": "Microsoft.App/environments"}},
                ]}},
                {"name": "cosmos", "properties": {"addressPrefix": "10.241.0.64/27", "privateEndpointNetworkPolicies": "Disabled"}},
            ]}})
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        for mutate in (
            lambda node: node["after"]["tags"].update(SecurityControl="Ignore"),
            lambda node: node["after"]["properties"]["addressSpace"].update(addressPrefixes=["10.242.0.0/25"]),
            lambda node: node["after"]["properties"]["subnets"][0]["properties"].update(delegations=[]),
        ):
            bad = deepcopy(raw)
            mutate(bad["changes"][0])
            self.assertTrue(PREVIEW.scope_preview(bad, bound, params)["blockingReasons"])

    def test_prepare_preview_never_authorizes_and_removes_private_parameters(self):
        bound, raw, params = fixture()
        params["secret"] = {"value": "synthetic-private-key-never-publish"}
        with tempfile.TemporaryDirectory() as directory:
            env = {**ENV, "GITHUB_RUN_ID": "123", "RUNNER_TEMP": directory, "AI4IA_RELEASE_KIND": "preview",
                   "GITHUB_OUTPUT": str(Path(directory) / "outputs"), "GITHUB_ENV": str(Path(directory) / "env")}
            observed = []
            def command(args, **kwargs):
                self.assertEqual(args[:4], ["az", "deployment", "group", "what-if"])
                self.assertIn("Provider", args)
                parameters = Path(args[args.index("--parameters") + 1][1:])
                self.assertIn(params["secret"]["value"], parameters.read_text())
                observed.append(parameters)
                return json.dumps(raw).encode()
            console = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "prepare"]), \
                 patch.object(PREVIEW, "compiled_context", return_value=({}, params, bound, None, params)), \
                 patch.object(PREVIEW, "run", side_effect=command), redirect_stdout(console):
                self.assertEqual(PREVIEW.main(), 0)
            self.assertEqual(len(observed), 1)
            self.assertFalse(observed[0].exists())
            self.assertIn("authorized=false", Path(env["GITHUB_OUTPUT"]).read_text())
            report = (Path(directory) / "ai4ia-private-preview/report.json").read_text()
            self.assertNotIn(params["secret"]["value"], report + console.getvalue())
            self.assertFalse(json.loads(report)["executionAuthorized"])
            self.assertFalse((Path(directory) / "ai4ia-private-preview/proof.json").exists())
            env["AI4IA_RELEASE_KIND"] = "deploy"
            env["AI4IA_PRIVATE_PREVIEW_BINDING"] = json.loads(report)["binding"]
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "prepare"]), \
                 patch.object(PREVIEW, "compiled_context", return_value=({}, params, bound, None, params)), \
                 patch.object(PREVIEW, "run", side_effect=command), redirect_stdout(console):
                self.assertEqual(PREVIEW.main(), 0)
            self.assertTrue(Path(env["GITHUB_OUTPUT"]).read_text().endswith("authorized=true\n"))
            self.assertTrue((Path(directory) / "ai4ia-private-preview/proof.json").exists())
            self.assertTrue(observed[-1].exists())
            retained = dict(line.split("=", 1) for line in Path(env["GITHUB_ENV"]).read_text().splitlines())
            with patch.dict(os.environ, {**env, **retained}, clear=True), \
                 patch.object(sys, "argv", ["preview", "cleanup"]), \
                 patch.object(PREVIEW, "run", side_effect=AssertionError("cleanup must stay offline")):
                self.assertEqual(PREVIEW.main(), 0)
            self.assertFalse(observed[-1].exists())
            changed = {**bound, "parameters": "d" * 64}
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "prepare"]), \
                 patch.object(PREVIEW, "compiled_context", return_value=({}, params, changed, None, params)), \
                 patch.object(PREVIEW, "run", side_effect=command), redirect_stdout(console):
                self.assertEqual(PREVIEW.main(), 1)
            self.assertTrue(Path(env["GITHUB_OUTPUT"]).read_text().endswith("authorized=false\n"))
            self.assertTrue(all(not path.exists() for path in observed))

    def test_new_api_is_staged_with_exact_identity_auth_data_and_compute(self):
        bound, raw, params = fixture()
        prefix = f"/subscriptions/{bound['subscription']}/resourceGroups/rg-ai4ia-test/providers/"
        params.update({"entraTenantId": {"value": "tenant"}, "entraAudience": {"value": "audience"},
                       "apiPrivateInitialImage": {"value": "inert-image"}})
        row = raw["changes"][-1]
        row.update(changeType="Create", after={
            "location": "eastus2", "identity": {"type": "UserAssigned", "userAssignedIdentities": {
                prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test": {},
            }},
            "properties": {
                "managedEnvironmentId": prefix + "Microsoft.App/managedEnvironments/cae-ai4ia-test-api-private",
                "configuration": {"activeRevisionsMode": "Single",
                                  "ingress": {"external": True, "targetPort": 8080, "allowInsecure": False}},
                "template": {"scale": {"minReplicas": 1, "maxReplicas": 1}, "containers": [{
                    "image": "inert-image", "resources": {"cpu": 0.5, "memory": "1Gi"},
                    "env": [{"name": name, "value": value} for name, value in {
                        "AI4IA_PRIVATE_RECOVERY_CANDIDATE": "true", "AI4IA_PRIVATE_COSMOS_REQUIRED": "true",
                        "AI4IA_COSMOS_ENDPOINT": "https://cosmos-test.documents.azure.com:443/",
                        "AI4IA_ENV": "prod", "AI4IA_AUTH_PROVIDER": "entra",
                        "AI4IA_ENTRA_TENANT_ID": "tenant", "AI4IA_ENTRA_AUDIENCE": "audience",
                    }.items()],
                }]},
            },
        })
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        for mutation in (
            lambda after: after["properties"]["template"]["containers"][0]["resources"].update(cpu=1),
            lambda after: after["properties"]["configuration"]["ingress"].update(allowInsecure=True),
            lambda after: after["properties"]["template"]["containers"][0]["env"][2].update(value="https://other.documents.azure.com"),
            lambda after: after["properties"]["template"]["containers"][0]["env"][-1].update(value="other-audience"),
        ):
            changed = deepcopy(raw)
            mutation(changed["changes"][-1]["after"])
            self.assertTrue(PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])

    def test_web_cutover_can_only_change_to_the_observed_private_origin(self):
        bound, raw, params = fixture()
        bound.update(phase="private", apiOrigin="https://ca-api-private-test.synthetic.azurecontainerapps.io")
        template = {"scale": {"minReplicas": 1, "maxReplicas": 3}, "containers": [{
            "name": "web", "image": "same-image", "env": [
                {"name": "API_BASE_URL", "value": "https://old.test"},
                {"name": "API_PUBLIC_URL", "value": "https://old.test"},
                {"name": "OTHER", "value": "unchanged"},
            ],
        }]}
        after = deepcopy(template)
        for row in after["containers"][0]["env"][:2]:
            row["value"] = bound["apiOrigin"]
        raw["changes"].append({
            "resourceId": f"/subscriptions/{bound['subscription']}/resourceGroups/rg-ai4ia-test/providers/Microsoft.App/containerApps/ca-web-test",
            "changeType": "Modify", "delta": [{"path": "properties.template.containers"}],
            "before": {"properties": {"template": template}}, "after": {"properties": {"template": after}},
        })
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        after["containers"][0]["env"][0]["value"] = "https://wrong.test"
        self.assertIn("web_voice_destination_drift", PREVIEW.scope_preview(raw, bound, params)["blockingReasons"])

    def test_scalar_renderer_preserves_defaults_and_refuses_unsupported_input(self):
        source = {"parameters": {"key": {"value": "${SECRET=}"}, "flag": {"value": "${FLAG=false}"}}}
        template = {"parameters": {"key": {"type": "securestring"}, "flag": {"type": "bool"},
                                   "optional": {"type": "array", "defaultValue": []}}}
        result = PREVIEW.resolve_parameters(source, template, {"SECRET": "not-printed", "FLAG": "true"})
        self.assertEqual(result, {"key": {"value": "not-printed"}, "flag": {"value": True}})
        for bad in ('quote"', "back\\slash", "line\nbreak", "$(command)"):
            with self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.resolve_parameters(source, template, {"SECRET": bad})
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.resolve_parameters(source, {**template, "parameters": {
                **template["parameters"], "newRequired": {"type": "string"},
            }}, {})

    def test_diagnostic_rejection_retains_only_safe_metadata_and_no_authority(self):
        bound, raw, params = fixture()
        secret = "sk_test_never_publish_this_private_value"
        params["secret"] = {"value": secret}
        raw["changes"][0].update(before={"private": secret}, after={"expression": secret},
                                 delta=[{"path": "properties.example", "before": secret, "after": secret}])
        valid = PREVIEW.scope_preview(raw, bound, params)
        old_binding = PREVIEW.make_token(bound, valid, int(PREVIEW.time.time()))
        diagnostics = [
            {"code": "WhatIfExampleDiagnostic", "level": "Warning",
             "target": raw["changes"][0]["resourceId"], "message": secret,
             "additionalInfo": [{"type": "Secret", "info": {"value": secret}}]},
            {"level": "Info", "target": "properties.template.containers[0].env",
             "message": f"unresolved expression {secret}"},
            {"code": secret, "level": {"unsafe": secret}, "target": "https://invalid.test/" + secret,
             "message": secret, "expression": secret},
        ]
        raw["diagnostics"] = diagnostics
        for kind in ("preview", "deploy"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                env = {**ENV, "GITHUB_RUN_ID": "123", "RUNNER_TEMP": directory, "AI4IA_RELEASE_KIND": kind,
                       "AI4IA_PRIVATE_PREVIEW_BINDING": old_binding,
                       "GITHUB_OUTPUT": str(Path(directory) / "outputs"),
                       "GITHUB_ENV": str(Path(directory) / "env")}
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "prepare"]), \
                     patch.object(PREVIEW, "compiled_context", return_value=({}, params, bound, None, params)), \
                     patch.object(PREVIEW, "run", return_value=json.dumps(raw).encode()), \
                     patch.object(PREVIEW, "scope_preview", wraps=PREVIEW.scope_preview) as scope, \
                     redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
                    self.assertEqual(PREVIEW.main(), 1)
                self.assertEqual(scope.call_args.args[0]["diagnostics"], diagnostics)
                path = Path(directory) / "ai4ia-private-preview/report.json"
                text = path.read_text()
                report = json.loads(text)
                self.assertEqual(report["status"], "rejected")
                self.assertEqual(report["diagnostics"][0]["code"], "WhatIfExampleDiagnostic")
                self.assertEqual(report["diagnostics"][0]["level"], "Warning")
                self.assertEqual(report["diagnostics"][0]["target"], raw["changes"][0]["resourceId"])
                self.assertEqual(report["diagnostics"][1]["code"], "unknown")
                self.assertEqual(report["diagnostics"][2]["level"], "unknown")
                self.assertIsNone(report["diagnostics"][2]["target"])
                self.assertIsNone(report["binding"])
                self.assertFalse(report["executionAuthorized"])
                self.assertNotIn(secret, text + stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("additionalInfo", text)
                self.assertNotIn("before", text)
                self.assertNotIn("after", text)
                self.assertEqual(Path(env["GITHUB_OUTPUT"]).read_text(), "authorized=false\n")
                self.assertFalse((path.parent / "proof.json").exists())
                self.assertEqual(list(Path(directory).glob("ai4ia-private-packet-*/parameters.json")), [])

    def test_unknown_diagnostic_shape_is_rejected_and_report_is_bounded(self):
        bound, raw, params = fixture()
        for shape in ({}, False, 0, "unknown"):
            raw["diagnostics"] = shape
            with self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.scope_preview(raw, bound, params)
            report = PREVIEW.rejected_report(raw, bound)
            self.assertTrue(report["diagnosticsShapeUnknown"])
            self.assertEqual(report["category"], "arm_diagnostics_shape_unknown")
            self.assertIsNone(report["binding"])
        raw["diagnostics"] = [{"level": "Warning", "code": "Example", "message": "never copy"}] * 100
        raw["changes"] *= 100
        report = PREVIEW.rejected_report(raw, bound)
        self.assertTrue(report["truncated"])
        self.assertLessEqual(len(report["diagnostics"]), 16)
        self.assertLessEqual(len(report["changes"]), 32)
        self.assertLessEqual(len(json.dumps(report, indent=2).encode()), 64 * 1024)

    def test_current_compiled_parameter_contract_is_covered(self):
        compiled = _compile()
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        template = json.loads(compiled.stdout)
        source = json.loads((ROOT / "infra/main.parameters.json").read_text())
        result = PREVIEW.resolve_parameters(source, template, {})
        self.assertEqual(set(result), set(source["parameters"]))
        self.assertGreater(len(result), 100)
        self.assertEqual(result["apiNetworkPhase"], {"value": "legacy"})


class OperationTests(unittest.TestCase):
    def test_pending_push_never_deploys_and_completed_state_is_explicit(self):
        env = {**ENV, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push"}
        for selected in ("candidate", "activate", "private"):
            self.assertEqual(deployment_operation({**env, "AI4IA_API_NETWORK_PHASE": selected},
                                                 pending=True, blocked=False), "preview")
        self.assertEqual(deployment_operation({**env, "AI4IA_API_NETWORK_PHASE": "private"},
                                             pending=False, blocked=False), "ordinary")
        for pending, blocked in ((None, False), (False, None), (0, False)):
            with self.assertRaises(ValueError):
                deployment_operation(env, pending=pending, blocked=blocked)
        with self.assertRaises(ValueError):
            deployment_operation({**env, "GITHUB_REF": "refs/heads/untrusted"}, pending=True, blocked=False)

    def test_preview_can_describe_a_blocked_transition_but_never_admit_it(self):
        cloud = Cloud()
        cloud.old[0]["properties"]["active"] = True
        env = {**ENV, "AI4IA_API_NETWORK_PHASE": "activate"}
        observed = MODULE.admission(env, cloud.read, lambda _: True, preview=True)
        self.assertIn("legacy_revisions_active", observed["AI4IA_PRIVATE_EXECUTION_BLOCKERS"])
        with self.assertRaises(ValueError):
            MODULE.admission(env, cloud.read, lambda _: True)
        requested = {**env, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                     "AI4IA_RELEASE_OPERATION": "deploy", "AI4IA_RELEASE_PROVISION": "true",
                     "AI4IA_PRIVATE_PREVIEW_BINDING": "reviewed"}
        with self.assertRaises(ValueError):
            deployment_operation(requested, pending=True, blocked=True)
        self.assertEqual(deployment_operation(requested, pending=True, blocked=False), "deploy")


if __name__ == "__main__":
    unittest.main()
