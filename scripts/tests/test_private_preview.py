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
    bound["locationIdentity"] = {
        "id": f"/subscriptions/{bound['subscription']}/locations/eastus2",
        "name": "eastus2", "displayName": "East US 2", "type": "Region",
    }
    vnet = prefix + "Microsoft.Network/virtualNetworks/vnet-ai4ia-test-api-private"
    endpoint = prefix + "Microsoft.Network/privateEndpoints/pe-ai4ia-test-api-private-cosmos"
    zone = prefix + "Microsoft.Network/privateDnsZones/privatelink.documents.azure.com"
    ids = [
        vnet, endpoint, endpoint + "/privateDnsZoneGroups/default", zone,
        zone + "/virtualNetworkLinks/ai4ia-test-api-private",
        prefix + "Microsoft.App/managedEnvironments/cae-ai4ia-test-api-private",
        prefix + "Microsoft.Network/networkSecurityGroups/vnet-ai4ia-test-api-private-api-nsg-eastus2",
        prefix + "Microsoft.Network/networkSecurityGroups/vnet-ai4ia-test-api-private-cosmos-nsg-eastus2",
        prefix + "Microsoft.App/containerApps/ca-api-private-test",
    ]
    raw = {"status": "Succeeded", "changes": [
        {"resourceId": rid, "changeType": "NoChange"} for rid in ids
    ]}
    for row in raw["changes"][6:8]:
        row["after"] = {"properties": {"securityRules": []}}
    params = {
        "apiPrivateVnetPrefix": {"value": "10.241.0.0/25"},
        "apiPrivateSubnetPrefix": {"value": "10.241.0.0/26"},
        "cosmosPrivateEndpointSubnetPrefix": {"value": "10.241.0.64/27"},
    }
    return bound, raw, params


def complete_foundation_fixture():
    """Complete network snapshot; native SDK/knack emits these null deltas."""
    bound, original, params = fixture()
    bound["packet"] = "foundation"
    vnet, endpoint, group, zone, link, environment = [
        row["resourceId"] for row in original["changes"][:6]
    ]
    prefix = vnet.split("/providers/")[0] + "/providers/"
    api_nsg, cosmos_nsg = [row["resourceId"] for row in original["changes"][6:8]]
    snapshots = [
        (vnet, "eastus2", {
            "addressSpace": {"addressPrefixes": ["10.241.0.0/25"]},
            "dhcpOptions": {"dnsServers": []},
            "subnets": [
                {"name": "api", "properties": {"addressPrefix": "10.241.0.0/26", "delegations": [
                    {"name": "container-apps", "properties": {"serviceName": "Microsoft.App/environments"}},
                ], "networkSecurityGroup": {"id": api_nsg}}},
                {"name": "cosmos", "properties": {"addressPrefix": "10.241.0.64/27",
                                                  "networkSecurityGroup": {"id": cosmos_nsg},
                                                  "privateEndpointNetworkPolicies": "Disabled"}},
            ],
        }),
        (endpoint, "eastus2", {
            "subnet": {"id": vnet + "/subnets/cosmos"},
            "privateLinkServiceConnections": [{"name": "cosmos-sql", "properties": {
                "privateLinkServiceId": prefix + "Microsoft.DocumentDB/databaseAccounts/cosmos-test",
                "groupIds": ["Sql"],
            }}],
        }),
        (group, None, {"privateDnsZoneConfigs": [
            {"name": "cosmos-sql", "properties": {"privateDnsZoneId": zone}},
        ]}),
        (zone, "global", {}),
        (link, "global", {"registrationEnabled": False, "virtualNetwork": {"id": vnet}}),
        (environment, "eastus2", {
            "vnetConfiguration": {"infrastructureSubnetId": vnet + "/subnets/api", "internal": False},
            "publicNetworkAccess": "Enabled",
            "appLogsConfiguration": {"destination": "log-analytics", "logAnalyticsConfiguration": {
                "customerId": "00000000-0000-0000-0000-000000000001", "sharedKey": "********",
            }},
            "workloadProfiles": [{"name": "Consumption", "workloadProfileType": "Consumption"}],
        }),
        (api_nsg, "eastus2", {"securityRules": []}),
        (cosmos_nsg, "eastus2", {"securityRules": []}),
    ]
    changes = []
    for resource_id, location, properties in snapshots:
        segments = resource_id.split("/providers/")[1].split("/")
        after = {
            "id": resource_id, "type": "/".join([segments[0], *segments[1::2]]),
            "name": "/".join(segments[2::2]), "properties": properties,
        }
        if location is not None:
            after.update(location=location, tags={"owner": "synthetic"})
        changes.append({"resourceId": resource_id, "changeType": "Create",
                        "before": None, "after": after, "delta": None, "unsupportedReason": None})
    for resource_id in (
        prefix + "Microsoft.App/containerApps/ca-api-test",
        prefix + "Microsoft.ApiManagement/service/existing-gateway",
    ):
        changes.append({"resourceId": resource_id, "changeType": "Ignore",
                        "before": None, "after": None, "delta": None, "unsupportedReason": None})
    return bound, {"status": "Succeeded", "error": None, "diagnostics": None, "changes": changes}, params


def foundation_echo_fixture():
    bound, raw, params = complete_foundation_fixture()
    bound["logAnalyticsCustomerId"] = "00000000-0000-0000-0000-000000000001"
    for row in raw["changes"][:8]:
        row.update(changeType="NoChange", before=deepcopy(row["after"]), delta=[])
    echoes = {
        0: {"properties.privateEndpointVNetPolicies": "Disabled"},
        1: {"properties.isIPv6EnabledPrivateEndpoint": False},
        4: {"properties.resolutionPolicy": "Default"},
        5: {
            "properties.appLogsConfiguration.logAnalyticsConfiguration.dynamicJsonColumns": False,
            "properties.peerAuthentication": {"mtls": {"enabled": False}},
            "properties.peerTrafficConfiguration": {"encryption": {"enabled": False}},
        },
    }
    for index, paths in echoes.items():
        row = raw["changes"][index]
        row["changeType"] = "Modify"
        for path, value in paths.items():
            keys = path.split(".")
            parent = row["before"]
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = deepcopy(value)
            row["delta"].append({"path": path, "propertyChangeType": "Delete",
                                 "before": deepcopy(value), "after": None, "children": None})
    row = raw["changes"][5]
    customer_path = "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId"
    prefix = row["resourceId"].split("/providers/")[0] + "/providers/"
    expression = f"[reference('{prefix}Microsoft.OperationalInsights/workspaces/log-ai4ia-test', '2023-09-01').customerId]"
    row["after"]["properties"]["appLogsConfiguration"]["logAnalyticsConfiguration"]["customerId"] = expression
    row["delta"].append({"path": customer_path, "propertyChangeType": "Modify",
                         "before": bound["logAnalyticsCustomerId"], "after": expression, "children": None})
    row = raw["changes"][2]
    old = row["before"]["properties"]["privateDnsZoneConfigs"][0]
    old.update(id=row["resourceId"] + "/privateDnsZoneConfigs/cosmos-sql",
               type="Microsoft.Network/privateEndpoints/privateDnsZoneGroups/privateDnsZoneConfigs",
               etag='W/"00000000-0000-0000-0000-000000000001"', resourceGroup=bound["resourceGroup"])
    old["properties"]["provisioningState"] = "Succeeded"
    row.update(changeType="Modify", delta=[{
        "path": "properties.privateDnsZoneConfigs", "propertyChangeType": "Array", "before": None, "after": None,
        "children": [{"path": "0", "propertyChangeType": "Modify", "before": None, "after": None, "children": [
            {"path": path, "propertyChangeType": "Delete", "before": PREVIEW.at_path(old, path),
             "after": None, "children": None}
            for path in ("id", "etag", "type", "properties.provisioningState")
        ]}],
    }])
    return bound, raw, params


ACTIVATION_FIELDS = [
    "properties.configuration.ingress.exposedPort",
    "properties.configuration.ingress.traffic",
    "properties.configuration.maxInactiveRevisions",
    "properties.patchingMode",
    "properties.template.containers",
    "properties.template.scale.cooldownPeriod",
    "properties.template.scale.maxReplicas",
    "properties.template.scale.pollingInterval",
    "properties.workloadProfileName",
]


def activation_set(resource, path, value=None, *, remove=False):
    keys = path.replace("[0]", ".0").split(".")
    parent = resource
    for key in keys[:-1]:
        parent = parent[int(key)] if isinstance(parent, list) else parent[key]
    if remove:
        parent.pop(keys[-1], None)
    else:
        parent[keys[-1]] = value


def activation_fixture():
    bound, _, params = fixture()
    bound.update(packet="activate", phase="activate")
    prefix = f"/subscriptions/{bound['subscription']}/resourceGroups/{bound['resourceGroup']}/providers/"
    rid = prefix + "Microsoft.App/containerApps/ca-api-private-test"
    before = {
        "id": rid, "type": "Microsoft.App/containerApps", "location": "eastus2", "tags": {},
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {
            prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test": {},
        }},
        "properties": {
            "managedEnvironmentId": prefix + "Microsoft.App/managedEnvironments/private",
            "latestRevisionName": "ca-api-private-test--ready", "latestReadyRevisionName": "ca-api-private-test--ready",
            "patchingMode": "Automatic", "workloadProfileName": "Consumption",
            "configuration": {
                "activeRevisionsMode": "Single", "maxInactiveRevisions": 100, "secrets": [],
                "ingress": {"external": True, "targetPort": 8000, "exposedPort": 0, "transport": "auto",
                            "traffic": [{"latestRevision": True, "weight": 100}]},
                "registries": [{"server": "synthetic.azurecr.io", "identity": prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test"}],
            },
            "template": {
                "scale": {"minReplicas": 1, "maxReplicas": 1, "cooldownPeriod": 300, "pollingInterval": 30},
                "containers": [{
                    "name": "api", "image": "synthetic.azurecr.io/ai4ia/api-test@sha256:" + "1" * 64,
                    "imageType": "ContainerImage", "resources": {"cpu": 0.5, "memory": "1Gi"},
                    "env": [
                        {"name": "AI4IA_PRIVATE_RECOVERY_CANDIDATE", "value": "true"},
                        {"name": "AI4IA_PRIVATE_COSMOS_REQUIRED", "value": "true"},
                        {"name": "UNCHANGED_SETTING", "value": "retained"},
                    ],
                }],
            },
        },
    }
    after, baseline, legacy = deepcopy(before), deepcopy(before), deepcopy(before)
    for path in ACTIVATION_FIELDS:
        if path not in ("properties.template.containers", "properties.template.scale.maxReplicas"):
            activation_set(after, path, remove=True)
    activation_set(after, "properties.template.containers[0].imageType", remove=True)
    after["properties"]["template"]["scale"]["maxReplicas"] = 3
    after["properties"]["template"]["containers"][0]["env"][0]["value"] = "false"
    for path in ("properties.patchingMode", "properties.workloadProfileName", "properties.template.containers[0].imageType"):
        activation_set(baseline, path, remove=True)
    legacy["id"] = prefix + "Microsoft.App/containerApps/ca-api-test"
    legacy["properties"].update(latestRevisionName="ca-api-test--ready", latestReadyRevisionName="ca-api-test--ready")
    legacy["properties"]["template"]["scale"]["maxReplicas"] = 3
    legacy["properties"]["configuration"]["maxInactiveRevisions"] = None
    legacy["properties"]["template"]["containers"][0]["env"] = [{"name": "UNCHANGED_SETTING", "value": "retained"}]
    raw = {"status": "Succeeded", "changes": [{
        "resourceId": rid, "changeType": "Modify", "before": before, "after": after,
        "delta": [{"path": path} for path in ACTIVATION_FIELDS],
    }]}
    return bound, raw, params, baseline, legacy


class PreviewTests(unittest.TestCase):
    def test_bounded_command_reports_safe_arm_class_code_target_and_message(self):
        error = {"error": {
            "code": "InvalidTemplate", "target": "apiApp",
            "message": "Deployment template validation failed: 'The resource 'private-target' at line '1' "
                       "and column '33' is defined multiple times in a template. Please see https://example.test/help.'",
        }}
        command = [sys.executable, "-c", "import sys;sys.stderr.write(" + repr("ERROR: " + json.dumps(error))
                   + ");raise SystemExit(1)"]
        with self.assertRaises(PREVIEW.PreviewError) as caught:
            PREVIEW.run(command, limit=1024, timeout=5)
        text = str(caught.exception)
        self.assertIn('"class":"arm_error"', text)
        self.assertIn('"code":"InvalidTemplate"', text)
        self.assertIn('"target":"apiApp"', text)
        self.assertIn("defined multiple times", text)
        self.assertNotIn("private-target", text)
        self.assertNotIn("example.test", text)

    def test_command_diagnostic_redacts_secrets_and_withholds_unknown_or_oversize_data(self):
        secrets = ("short-api", "short-s7p", "short-client", "short-connection", "short-quoted",
                   "opaque" * 8, "eyJabc.eyJdef.signature123", "signed-query", "target-secret")
        message = (
            f"api_key='{secrets[0]}' S7P-KEY={secrets[1]} clientSecret={secrets[2]} "
            f"connection_string='{secrets[3]}' supplied value '{secrets[4]}'; "
            f"{secrets[5]} Authorization: Bearer {secrets[6]} "
            f"https://example.test/path?sig={secrets[7]}"
        )
        payload = {"code": "InvalidTemplate", "target": secrets[8], "message": message}
        diagnostic = PREVIEW.command_diagnostic(["az", "deployment", "group", "create"], "command_failed",
                                                json.dumps(payload).encode())
        encoded = json.dumps(diagnostic)
        self.assertEqual(diagnostic["operation"], "azure_deployment_create")
        self.assertEqual(diagnostic["target"], "[REDACTED_TARGET]")
        for secret in secrets:
            self.assertNotIn(secret, encoded)
        for raw in (b"sensitive-unstructured-value", b"x" * 16385, b"\xff", b'{"code":"secret-code"}',
                    b'{"code":"InvalidTemplate","code":"AuthorizationFailed","message":"private"}'):
            result = PREVIEW.command_diagnostic(["az"], "command_failed", raw)
            self.assertIsNone(result["code"])
            self.assertNotIn("sensitive-unstructured-value", json.dumps(result))
            self.assertNotIn("private", json.dumps(result))
        for kind in ("timeout", "stdout_limit", "stream_unavailable"):
            result = PREVIEW.command_diagnostic(["az"], kind, json.dumps(payload).encode())
            self.assertEqual(result["class"], kind)
            self.assertIsNone(result["code"])
        plain = PREVIEW.command_diagnostic(["az"], "command_failed",
                                           b"ERROR: (InvalidTemplate) Invalid value 'do-not-print'\nCode: InvalidTemplate")
        self.assertEqual(plain["code"], "InvalidTemplate")
        self.assertNotIn("do-not-print", json.dumps(plain))

    def test_real_bounded_stderr_does_not_change_success_timeout_or_failure_admission(self):
        success = [sys.executable, "-c", "import sys;sys.stderr.write('private-warning');sys.stdout.write('ok')"]
        self.assertEqual(PREVIEW.run(success, limit=2, timeout=5), b"ok")
        with self.assertRaisesRegex(PREVIEW.PreviewError, '"class":"stdout_limit"'):
            PREVIEW.run(success, limit=1, timeout=5)
        with self.assertRaisesRegex(PREVIEW.PreviewError, '"class":"timeout"'):
            PREVIEW.run([sys.executable, "-c", "import time;time.sleep(5)"], limit=10, timeout=1)
        flood = [sys.executable, "-c", "import sys;sys.stderr.write('secret-flood'*10000);raise SystemExit(1)"]
        with self.assertRaises(PREVIEW.PreviewError) as caught:
            PREVIEW.run(flood, limit=1024, timeout=5)
        self.assertNotIn("secret-flood", str(caught.exception))
        self.assertIn('"code":null', str(caught.exception))

    def test_activation_routes_only_to_patch_and_full_arm_constructor_refuses_it(self):
        for phase in ("activate", "private"):
            env = {**ENV, "AI4IA_API_NETWORK_PHASE": phase, "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40}
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "apply"]), \
                 patch.object(PREVIEW, "activation_patch_command", return_value=0) as patcher, \
                 patch.object(PREVIEW, "compiled_context", side_effect=AssertionError("Full ARM app update forbidden")):
                self.assertEqual(PREVIEW.main(), 0)
                patcher.assert_called_once()
            with patch.object(PREVIEW, "compile_module", side_effect=AssertionError("No ARM compilation")):
                with self.assertRaises(PREVIEW.PreviewError):
                    PREVIEW.compiled_context(env)

    def test_full_arm_activation_preview_is_retired_even_for_the_previously_approved_shape(self):
        bound, raw, params, baseline, _ = activation_fixture()
        with self.assertRaisesRegex(PREVIEW.PreviewError, "Unknown scoped recovery packet"):
            PREVIEW.scope_preview(raw, bound, params, baseline)

    def test_post_create_metadata_gate_precedes_success_and_contains_only_candidate(self):
        from scripts.tests.test_private_metadata import live_snapshot

        descriptor = {
            "secrets": [{"name": "model-gateway-api-key", "kind": "containerApp"}],
            "envSecretRefs": {"AI4IA_MODEL_GATEWAY_API_KEY": "model-gateway-api-key"},
            "envValueNames": [], "emptyEnvValues": [], "valueOrigins": [],
        }
        for fault in ("none", "metadata", "read", "containment", "missing-proof", "flags"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                packet = root / "ai4ia-private-packet-123-test"
                packet.mkdir()
                output = root / "ai4ia-private-preview"
                output.mkdir()
                template, parameters = {"resources": []}, {}
                bound, _, expected = fixture()
                bound.update(packet="candidate", template=PREVIEW.digest(template), parameters=PREVIEW.digest(parameters),
                             secretMetadataDigest=PREVIEW.digest(descriptor))
                expected["sourceSecretMetadata"] = deepcopy(descriptor)
                if fault == "missing-proof":
                    expected.pop("sourceSecretMetadata")
                preview = {"changes": [], "nsgComparisons": [], "blockingReasons": []}
                token = PREVIEW.make_token(bound, preview, int(PREVIEW.time.time()))
                proof = output / "proof.json"
                proof.write_text(json.dumps({"context": bound, "preview": preview}))
                (packet / "template.json").write_text(json.dumps(template))
                (packet / "parameters.json").write_text(json.dumps({"parameters": parameters}))
                env = {**ENV, "RUNNER_TEMP": directory, "GITHUB_RUN_ID": "123", "GITHUB_SHA": bound["source"],
                       "GITHUB_REPOSITORY": "ian-t-adams/AI4IA", "AI4IA_RELEASE_KIND": "deploy",
                       "AI4IA_RELEASE_PROVISION": "true", "AI4IA_PRIVATE_PREVIEW_BINDING": token,
                       "AI4IA_PRIVATE_PACKET_DIR": str(packet), "AI4IA_PRIVATE_PREVIEW_PROOF": str(proof),
                       "GITHUB_OUTPUT": str(root / "step-output")}
                sequence = []
                active = True
                rid = (f"/subscriptions/{bound['subscription']}/resourceGroups/{bound['resourceGroup']}"
                       "/providers/Microsoft.App/containerApps/ca-api-private-test")

                def read(args):
                    if args[0] == "rest":
                        sequence.append("metadata")
                        self.assertIn(rid + "?api-version=2024-10-02-preview", args[args.index("--url") + 1])
                        self.assertEqual(args[-1], PREVIEW.RAW_METADATA_QUERY)
                        if fault == "read":
                            raise PREVIEW.PreviewError("Metadata unavailable.")
                        result = live_snapshot(descriptor, rid)
                        if fault in ("metadata", "containment"):
                            result["secrets"][0]["name"] = "other-secret"
                        elif fault == "flags":
                            result["flags"][0]["value"] = "false"
                        return result
                    self.assertEqual(args[:3], ["containerapp", "revision", "list"])
                    self.assertEqual(args[args.index("-n") + 1], "ca-api-private-test")
                    if fault == "containment":
                        raise PREVIEW.PreviewError("Containment inventory unavailable.")
                    return [{"name": "ca-api-private-test--candidate", "active": active}] if active or "--all" in args else []

                def execute(args, **kwargs):
                    nonlocal active
                    if args[0] == "gh":
                        return json.dumps({"object": {"sha": bound["source"]}}).encode()
                    if args[:4] == ["az", "deployment", "group", "create"]:
                        sequence.append("create")
                        return b""
                    self.assertEqual(args[:4], ["az", "containerapp", "revision", "deactivate"])
                    self.assertEqual(args[args.index("-n") + 1], "ca-api-private-test")
                    self.assertEqual(args[args.index("--subscription") + 1], bound["subscription"])
                    self.assertEqual(args[args.index("--revision") + 1], "ca-api-private-test--candidate")
                    sequence.append("deactivate")
                    active = False
                    return b""

                claim = SimpleNamespace(returncode=1, stdout=b"", stderr=b"ERROR: (DeploymentNotFound) absent")
                with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "apply"]), \
                     patch.object(PREVIEW, "compiled_context", return_value=(template, parameters, bound, None, expected)), \
                     patch.object(PREVIEW, "cloud_read", side_effect=read), patch.object(PREVIEW, "run", side_effect=execute), \
                     patch.object(PREVIEW.subprocess, "run", return_value=claim), \
                     patch.object(PREVIEW.ADMISSION, "admission"), redirect_stdout(io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
                    self.assertEqual(PREVIEW.main(), 0 if fault == "none" else 1)
                if fault == "missing-proof":
                    self.assertEqual(sequence, [])
                    self.assertFalse((output / "execution.json").exists())
                else:
                    self.assertEqual(sequence[:2], ["create", "metadata"])
                    receipt = json.loads((output / "execution.json").read_text())
                    self.assertEqual(receipt["candidateSecretMetadata"]["status"], "verified" if fault == "none" else "failed")
                    self.assertIn("receipt=true", (root / "step-output").read_text())
                    if fault == "none":
                        self.assertEqual(sequence, ["create", "metadata"])
                    elif fault == "containment":
                        self.assertEqual(receipt["candidateContainment"]["status"], "unknown")
                    else:
                        self.assertEqual(sequence, ["create", "metadata", "deactivate"])
                        self.assertFalse(active)
                        self.assertEqual(receipt["candidateContainment"]["status"], "inactive")
                        self.assertFalse(receipt["candidateContainment"]["legacyTouched"])

    def test_exact_foundation_echoes_preserve_reported_deltas_and_binding(self):
        bound, raw, params = foundation_echo_fixture()
        report = PREVIEW.scope_preview(raw, bound, params)
        self.assertEqual(report["blockingReasons"], [])
        self.assertEqual(len(report["changes"]), 5)
        self.assertTrue(all(row["classification"] == "reviewed_source_identical_echo" for row in report["changes"]))
        self.assertEqual(report["rawChangeDigest"],
                         PREVIEW.digest(sorted(raw["changes"], key=lambda row: row["resourceId"].casefold())))
        token = PREVIEW.make_token(bound, report, 1792860000)
        PREVIEW.verify_token(token, bound, report, 1792860010)
        altered = deepcopy(bound)
        altered["logAnalyticsCustomerId"] = "00000000-0000-0000-0000-000000000002"
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.verify_token(token, altered, report, 1792860010)

    def test_each_echo_rejects_wrong_before_present_after_or_unlisted_path(self):
        bound, raw, params = foundation_echo_fixture()
        for index in (0, 1, 4, 5):
            for delta_index, original in enumerate(raw["changes"][index]["delta"]):
                for mutation in ("before", "after", "path", "unreported", "duplicate", "type"):
                    changed = deepcopy(raw)
                    row = changed["changes"][index]
                    delta = row["delta"][delta_index]
                    keys = original["path"].split(".")
                    with self.subTest(path=original["path"], mutation=mutation):
                        if mutation in ("before", "after"):
                            parent = row[mutation]
                            for key in keys[:-1]:
                                parent = parent.setdefault(key, {})
                            value = True if mutation == "before" else deepcopy(original["before"])
                            if mutation == "after" and original["path"].endswith(".customerId"):
                                value = "00000000-0000-0000-0000-000000000002"
                            parent[keys[-1]] = value
                            delta[mutation] = value
                        elif mutation == "path":
                            delta["path"] = "properties.unreviewed"
                        elif mutation == "unreported":
                            row["after"]["properties"]["unreported"] = False
                        elif mutation == "duplicate":
                            row["delta"].append(deepcopy(delta))
                        else:
                            row["after"]["type"] = "Microsoft.Network/unapproved"
                        self.assertIn("unapproved_modify", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])
        changed = deepcopy(raw)
        row = changed["changes"][1]
        row["before"]["properties"]["isIPv6EnabledPrivateEndpoint"] = 0
        row["delta"][0]["before"] = 0
        self.assertIn("unapproved_modify", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])

    def test_dns_echo_requires_equal_intent_and_exact_metadata(self):
        bound, raw, params = foundation_echo_fixture()
        for mutation in ("name", "zone", "state", "id", "etag", "type", "extra", "delta", "records"):
            changed = deepcopy(raw)
            row = changed["changes"][2]
            old, new = row["before"]["properties"]["privateDnsZoneConfigs"][0], row["after"]["properties"]["privateDnsZoneConfigs"][0]
            if mutation == "name":
                old["name"] = new["name"] = "other"
            elif mutation == "zone":
                old["properties"]["privateDnsZoneId"] = new["properties"]["privateDnsZoneId"] = "other"
            elif mutation == "state":
                old["properties"]["provisioningState"] = "Failed"
            elif mutation in ("id", "etag", "type"):
                old[mutation] = "other"
            elif mutation == "extra":
                old["properties"]["unreviewed"] = False
            elif mutation == "delta":
                row["delta"][0]["children"][0]["children"][0]["path"] = "properties.unreviewed"
            else:
                old["properties"]["recordSets"] = None
            with self.subTest(mutation=mutation):
                self.assertIn("unapproved_modify", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])
        changed = deepcopy(raw)
        row = changed["changes"][2]
        row["before"]["properties"]["privateDnsZoneConfigs"][0]["properties"]["recordSets"] = []
        row["delta"][0]["children"][0]["children"].append({
            "path": "properties.recordSets", "propertyChangeType": "Delete", "before": [], "after": None, "children": None,
        })
        self.assertEqual(PREVIEW.scope_preview(changed, bound, params)["blockingReasons"], [])

    def test_omitted_native_nsg_rules_use_scoped_raw_get_and_bind_its_digest(self):
        bound, raw, params = foundation_echo_fixture()
        snapshots = {}
        for index, name in ((6, "api"), (7, "cosmos")):
            row = raw["changes"][index]
            for side in ("before", "after"):
                row[side].pop("properties")
            rid = row["resourceId"].casefold()
            snapshots[rid] = {
                "id": rid, "etag": 'W/"00000000-0000-0000-0000-000000000001"', "location": "East US 2",
                "state": "Succeeded", "rulesPresent": True, "rules": [],
                "subnets": [raw["changes"][0]["resourceId"].upper() + "/subnets/" + name.upper()],
            }
        calls = []
        def read(args):
            calls.append(args)
            self.assertEqual(args[:3], ["rest", "--method", "get"])
            self.assertEqual(args[args.index("--subscription") + 1], bound["subscription"])
            url = args[args.index("--url") + 1]
            self.assertTrue(url.startswith("https://management.azure.com/"))
            self.assertTrue(url.endswith("?api-version=2024-05-01"))
            rid = url.removeprefix("https://management.azure.com").split("?")[0]
            value = deepcopy(snapshots[rid])
            native = {
                "id": value["id"], "etag": value["etag"], "location": value["location"],
                "properties": {"provisioningState": value["state"],
                               "subnets": [{"id": subnet} for subnet in value["subnets"]]},
            }
            if value["rulesPresent"]:
                native["properties"]["securityRules"] = value["rules"]
            return jmespath.search(args[args.index("--query") + 1], native)
        observations = []
        with patch.object(PREVIEW.time, "time", return_value=1792860000):
            report = PREVIEW.scope_preview(raw, bound, params, read=read, observations=observations)
        self.assertEqual(report["blockingReasons"], [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(row["snapshotState"] == "properties_absent" and row["customRulesEmpty"]
                            and row["liveReadDigest"] for row in report["nsgComparisons"]))
        self.assertTrue(all(row["observedAt"] == 1792860000 for row in observations))
        self.assertEqual(len(PREVIEW.verify_nsg_reads(report, bound, read)), 2)
        saved = deepcopy(snapshots)
        for field, value in (("rules", None), ("rules", [{}]), ("rules", False), ("rulesPresent", False),
                             ("subnets", []), ("state", "Failed"), ("id", "foreign"), ("location", "westus")):
            snapshots.clear()
            snapshots.update(deepcopy(saved))
            snapshots[next(iter(snapshots))][field] = value
            with self.subTest(field=field, value=value), self.assertRaises((PREVIEW.PreviewError, ValueError)):
                PREVIEW.scope_preview(raw, bound, params, read=read)
        snapshots.clear()
        snapshots.update(deepcopy(saved))
        snapshots[next(iter(snapshots))]["etag"] = "changed"
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.verify_nsg_reads(report, bound, read)
        for shape in (None, {"securityRules": None}, {"securityRules": [{}]}):
            changed = deepcopy(raw)
            changed["changes"][6]["after"]["properties"] = shape
            changed["changes"][7]["after"]["properties"] = {"securityRules": []}
            with patch.object(PREVIEW, "read_empty_nsg", side_effect=AssertionError("must not rescue null/custom rules")):
                self.assertIn("nsg_rules_not_empty_or_unknown",
                              PREVIEW.scope_preview(changed, bound, params, read=read)["blockingReasons"])
        changed = deepcopy(raw)
        for index in (6, 7):
            changed["changes"][index]["after"]["properties"] = {"securityRules": []}
        changed["changes"][6]["before"]["properties"] = {"securityRules": [{}]}
        self.assertIn("nsg_before_rules_nonempty_or_unknown",
                      PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])

    def test_foundation_nsgs_require_complete_default_only_rules(self):
        bound, raw, params = complete_foundation_fixture()
        for kind in ("Create", "NoChange"):
            for index in (6, 7):
                control = deepcopy(raw)
                control["changes"][index]["changeType"] = kind
                report = PREVIEW.scope_preview(control, bound, params)
                self.assertEqual(report["blockingReasons"], [])
                self.assertEqual(report["nsgComparisons"], [
                    {"resourceId": row["resourceId"].casefold(), "customRulesEmpty": True,
                     "snapshotState": "rules_empty"}
                    for row in control["changes"][6:8]
                ])
                for rules in (None, False, "", {}, [{"name": "private-marker"}]):
                    changed = deepcopy(control)
                    changed["changes"][index]["after"]["properties"]["securityRules"] = rules
                    with self.subTest(kind=kind, index=index, rules=rules):
                        denied = PREVIEW.scope_preview(changed, bound, params)
                        self.assertIn("nsg_rules_not_empty_or_unknown", denied["blockingReasons"])
                        self.assertNotIn("private-marker", json.dumps(denied))
                for missing in ("after", "properties", "securityRules"):
                    changed = deepcopy(control)
                    row = changed["changes"][index]
                    if missing == "after":
                        row.pop("after")
                    elif missing == "properties":
                        row["after"].pop("properties")
                    else:
                        row["after"]["properties"].pop("securityRules")
                    self.assertIn("nsg_rules_not_empty_or_unknown",
                                  PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])

    def test_foundation_nsg_associations_are_exact_not_arbitrary_attachments(self):
        bound, raw, params = complete_foundation_fixture()
        for index in (0, 1):
            expected = raw["changes"][6 + index]["resourceId"]
            control = deepcopy(raw)
            control["changes"][0]["after"]["properties"]["subnets"][index]["properties"]["networkSecurityGroup"] = {
                "id": expected.upper(),
            }
            self.assertEqual(PREVIEW.scope_preview(control, bound, params)["blockingReasons"], [])
            for attachment in (None, {}, {"id": raw["changes"][7 - index]["resourceId"]},
                               {"id": expected.replace("rg-ai4ia-test", "rg-other")},
                               {"id": expected + "-other"}, {"id": True}):
                changed = deepcopy(control)
                changed["changes"][0]["after"]["properties"]["subnets"][index]["properties"]["networkSecurityGroup"] = attachment
                with self.subTest(index=index, attachment=attachment):
                    self.assertIn("subnet_nsg_drift", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])
            changed = deepcopy(control)
            changed["changes"][0]["after"]["properties"]["subnets"][index]["properties"].pop("networkSecurityGroup")
            self.assertIn("subnet_nsg_drift", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])

    def test_nsg_adoption_never_authorizes_rule_removal_or_subnet_detachment(self):
        bound, raw, params = complete_foundation_fixture()
        for index in (6, 7):
            changed = deepcopy(raw)
            row = changed["changes"][index]
            row.update(changeType="Modify", before=deepcopy(row["after"]),
                       delta=[{"path": "properties.securityRules", "propertyChangeType": "Delete"}])
            row["before"]["properties"]["securityRules"] = [{"name": "private-marker"}]
            self.assertIn("unapproved_nsg_modify", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])
            for kind in ("Ignore", "Delete"):
                row["changeType"] = kind
                if kind == "Ignore":
                    with self.assertRaises(PREVIEW.PreviewError):
                        PREVIEW.scope_preview(changed, bound, params)
                else:
                    self.assertTrue(PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])
        detached = deepcopy(raw)
        row = detached["changes"][0]
        row.update(changeType="Modify", before=deepcopy(row["after"]),
                   delta=[{"path": "properties.subnets", "propertyChangeType": "Array"}])
        row["after"]["properties"]["subnets"][0]["properties"].pop("networkSecurityGroup")
        self.assertIn("unapproved_modify", PREVIEW.scope_preview(detached, bound, params)["blockingReasons"])

    def test_complete_new_vnet_default_dns_omission_keeps_exact_network_proof(self):
        bound, raw, params = complete_foundation_fixture()
        explicit = PREVIEW.scope_preview(raw, bound, params)
        del raw["changes"][0]["after"]["properties"]["dhcpOptions"]
        omitted = PREVIEW.scope_preview(raw, bound, params)
        for report in (explicit, omitted):
            self.assertEqual(report["blockingReasons"], [])
            self.assertEqual(len(report["changes"]), 8)
            network = next(row for row in report["changes"] if "networkComparisons" in row)
            self.assertEqual(network["networkComparisons"], {
                "addressSpaceMatches": True, "subnetCount": 2, "subnetCountCapped": False,
                "subnetRowsValid": True, "subnetNamesMatch": True, "apiPrefixMatches": True,
                "endpointPrefixMatches": True, "apiDelegationMatches": True, "endpointPolicyMatches": True,
                "apiNsgMatches": True, "endpointNsgMatches": True,
            })
        old = next(row for row in explicit["changes"] if "dnsComparison" in row)["dnsComparison"]
        new = next(row for row in omitted["changes"] if "dnsComparison" in row)["dnsComparison"]
        self.assertEqual(old, {"optionsPresent": True, "representation": "explicit-empty",
                               "serverCount": 0, "serverCountCapped": False})
        self.assertEqual(new, {"optionsPresent": False, "representation": "omitted-create-default",
                               "serverCount": None, "serverCountCapped": False})
        self.assertNotEqual(explicit["rawChangeDigest"], omitted["rawChangeDigest"])

    def test_omitted_dns_does_not_mask_missing_or_wrong_subnet_evidence(self):
        bound, original, params = complete_foundation_fixture()
        del original["changes"][0]["after"]["properties"]["dhcpOptions"]
        mutations = (
            ("subnet_inventory_invalid", lambda props: props.pop("subnets")),
            ("subnet_inventory_invalid", lambda props: props.update(subnets=None)),
            ("subnet_inventory_invalid", lambda props: props.update(subnets=[None, None])),
            ("subnet_count_drift", lambda props: props["subnets"].pop()),
            ("subnet_names_drift", lambda props: props["subnets"][0].update(name="unexpected-private-name")),
            ("subnet_prefix_drift", lambda props: props["subnets"][0]["properties"].update(addressPrefix="10.242.0.0/26")),
            ("subnet_prefix_drift", lambda props: props["subnets"][1]["properties"].update(addressPrefix="10.242.0.64/27")),
            ("api_subnet_delegation_drift", lambda props: props["subnets"][0]["properties"].update(delegations=[])),
            ("api_subnet_delegation_drift", lambda props: props["subnets"][0]["properties"].update(delegations=None)),
            ("endpoint_subnet_policy_drift", lambda props: props["subnets"][1]["properties"].update(privateEndpointNetworkPolicies="Enabled")),
            ("unapproved_subnet_attachment", lambda props: props["subnets"][0]["properties"].update(natGateway={"id": "unapproved"})),
            ("subnet_dns_override", lambda props: props["subnets"][0]["properties"].update(dhcpOptions={"dnsServers": ["private-marker"]})),
            ("vnet_address_drift", lambda props: props.pop("addressSpace")),
        )
        for expected, mutate in mutations:
            payload = deepcopy(original)
            mutate(payload["changes"][0]["after"]["properties"])
            with self.subTest(expected=expected):
                report = PREVIEW.scope_preview(payload, bound, params)
                self.assertIn(expected, report["blockingReasons"])
                self.assertNotIn("subnet_or_dns_drift", report["blockingReasons"])
                self.assertNotIn("private-marker", json.dumps(report))
                self.assertNotIn("unexpected-private-name", json.dumps(report))
                with self.assertRaises(PREVIEW.PreviewError):
                    PREVIEW.verify_token(PREVIEW.make_token(bound, report, 1792860000), bound, report, 1792860010)

    def test_custom_or_malformed_dns_is_not_a_create_default(self):
        bound, original, params = complete_foundation_fixture()
        for dns in (None, False, 0, "", [], {}, {"dnsServers": None}, {"dnsServers": False},
                    {"dnsServers": [], "unknown": "private-marker"}, {"dnsServers": ["private-marker"]}):
            payload = deepcopy(original)
            payload["changes"][0]["after"]["properties"]["dhcpOptions"] = dns
            with self.subTest(dns=dns):
                report = PREVIEW.scope_preview(payload, bound, params)
                reason = "vnet_dns_not_default" if dns == {"dnsServers": ["private-marker"]} else "vnet_dns_shape_invalid"
                self.assertIn(reason, report["blockingReasons"])
                self.assertNotIn("private-marker", json.dumps(report))
                network = next(row for row in report["changes"] if "dnsComparison" in row)
                self.assertTrue(network["networkComparisons"]["subnetNamesMatch"])
                self.assertTrue(network["networkComparisons"]["apiPrefixMatches"])
                self.assertTrue(network["networkComparisons"]["endpointPrefixMatches"])
        oversized = deepcopy(original)
        oversized["changes"][0]["after"]["properties"]["dhcpOptions"]["dnsServers"] = ["private-marker"] * 129
        report = PREVIEW.scope_preview(oversized, bound, params)
        dns = next(row for row in report["changes"] if "dnsComparison" in row)["dnsComparison"]
        self.assertEqual(dns["serverCount"], 128)
        self.assertIs(dns["serverCountCapped"], True)
        self.assertNotIn("private-marker", json.dumps(report))

    def test_create_default_does_not_authorize_existing_vnet_dns_removal(self):
        bound, raw, params = complete_foundation_fixture()
        row = raw["changes"][0]
        row.update(changeType="Modify", before=deepcopy(row["after"]),
                   delta=[{"path": "properties.dhcpOptions", "propertyChangeType": "Delete"}])
        row["before"]["properties"]["dhcpOptions"]["dnsServers"] = ["private-marker"]
        del row["after"]["properties"]["dhcpOptions"]
        report = PREVIEW.scope_preview(raw, bound, params)
        self.assertIn("unapproved_modify", report["blockingReasons"])
        self.assertNotIn("private-marker", json.dumps(report))

    def test_omitted_vnet_dns_keeps_endpoint_and_private_dns_guards(self):
        bound, raw, params = complete_foundation_fixture()
        del raw["changes"][0]["after"]["properties"]["dhcpOptions"]
        for index, mutate, expected in (
            (1, lambda props: props["privateLinkServiceConnections"][0]["properties"].update(groupIds=["MongoDB"]),
             "cosmos_endpoint_drift"),
            (1, lambda props: props["subnet"].update(id="unapproved"), "cosmos_endpoint_drift"),
            (2, lambda props: props["privateDnsZoneConfigs"][0]["properties"].update(privateDnsZoneId="unapproved"),
             "dns_zone_group_drift"),
            (4, lambda props: props.update(registrationEnabled=True), "dns_link_drift"),
        ):
            changed = deepcopy(raw)
            mutate(changed["changes"][index]["after"]["properties"])
            with self.subTest(expected=expected):
                self.assertIn(expected, PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])

    def test_complete_network_create_native_envelope_accepts_only_optional_create_delta(self):
        bound, raw, params = complete_foundation_fixture()
        for form in ("null", "absent", "array"):
            payload = deepcopy(raw)
            for row in payload["changes"][:8]:
                if form == "absent":
                    row.pop("delta")
                elif form == "array":
                    row["delta"] = []
            with self.subTest(form=form):
                report = PREVIEW.scope_preview(payload, bound, params)
                self.assertEqual(report["blockingReasons"], [])
                self.assertEqual(len(report["changes"]), 8)
                self.assertTrue(all(row["changeType"] == "Create" and row["fields"] for row in report["changes"]))
                self.assertNotIn("********", json.dumps(report))
                token = PREVIEW.make_token(bound, report, 1792860000)
                PREVIEW.verify_token(token, bound, report, 1792860010)

    def test_create_wrong_delta_types_and_missing_snapshots_still_refuse(self):
        bound, raw, params = complete_foundation_fixture()
        for value in (False, 0, "", "private-marker", {}, {"private": "marker"}, [None], ["private-marker"]):
            payload = deepcopy(raw)
            payload["changes"][0]["delta"] = value
            with self.subTest(delta=value), self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.scope_preview(payload, bound, params)
        for value in (None, [], "private-marker", {}):
            payload = deepcopy(raw)
            payload["changes"][0]["after"] = value
            with self.subTest(after=value):
                try:
                    result = PREVIEW.scope_preview(payload, bound, params)
                except ValueError:
                    continue
                self.assertTrue(result["blockingReasons"])
                with self.assertRaises(PREVIEW.PreviewError):
                    PREVIEW.verify_token(PREVIEW.make_token(bound, result, 1792860000), bound, result, 1792860010)
        for missing in range(8):
            payload = deepcopy(raw)
            payload["changes"].pop(missing)
            with self.subTest(missing=missing), self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.scope_preview(payload, bound, params)

    def test_modify_requires_complete_delta_even_when_create_delta_is_optional(self):
        bound, raw, params = complete_foundation_fixture()
        row = raw["changes"][0]
        row.update(changeType="Modify", before=deepcopy(row["after"]),
                   delta=[{"path": "tags.SecurityControl", "propertyChangeType": "Delete"}])
        row["before"]["tags"]["SecurityControl"] = "Ignore"
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        for value in (None, [], False, 0, "", {}, [None]):
            payload = deepcopy(raw)
            payload["changes"][0]["delta"] = value
            with self.subTest(delta=value), self.assertRaises(PREVIEW.PreviewError):
                PREVIEW.scope_preview(payload, bound, params)
        raw["changes"][0].pop("delta")
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.scope_preview(raw, bound, params)

    def test_rejected_delta_evidence_contains_only_presence_and_type(self):
        bound, raw, _ = complete_foundation_fixture()
        for value, expected in ((None, "null"), ([], "array"), ({}, "object"),
                                ({"nested": {"value": "private-value-do-not-publish"}}, "object"), (False, "boolean"),
                                (123, "number"), ("private-value-do-not-publish", "string")):
            payload = deepcopy(raw)
            payload["changes"][0]["delta"] = value
            report = PREVIEW.rejected_report(payload, bound)
            self.assertIs(report["changes"][0]["deltaPresent"], True)
            self.assertEqual(report["changes"][0]["deltaType"], expected)
            self.assertNotIn("private-value-do-not-publish", json.dumps(report))
            self.assertIsNone(report["binding"])
            self.assertIs(report["executionAuthorized"], False)
        raw["changes"][0].pop("delta")
        row = PREVIEW.rejected_report(raw, bound)["changes"][0]
        self.assertIs(row["deltaPresent"], False)
        self.assertEqual(row["deltaType"], "absent")

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
            "id": prefix + "Microsoft.App/containerApps/ca-api-test", "location": "East US 2",
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
               "AZURE_TENANT_ID": "tenant", "AZURE_LOCATION": "eastus2", "AI4IA_PRIVATE_STEP": "foundation",
               "AI4IA_PRIVATE_COSMOS_ENDPOINT": "https://cosmos-test.documents.azure.com:443/"}
        calls = []
        def read(args):
            calls.append(args)
            if args[:3] == ["monitor", "log-analytics", "workspace"]:
                self.assertEqual(args[args.index("--subscription") + 1], ENV["AZURE_SUBSCRIPTION_ID"])
                self.assertEqual(args[args.index("-g") + 1], "rg-ai4ia-test")
                self.assertEqual(args[args.index("-n") + 1], "log-ai4ia-test")
                return {"id": values["logAnalyticsWorkspaceId"], "customerId": "00000000-0000-0000-0000-000000000001"}
            if args[0] == "rest":
                self.assertEqual(args[args.index("--subscription") + 1], ENV["AZURE_SUBSCRIPTION_ID"])
                self.assertEqual(args[args.index("--url") + 1],
                    f"https://management.azure.com/subscriptions/{ENV['AZURE_SUBSCRIPTION_ID']}/locations?api-version=2022-12-01")
                return jmespath.search(args[args.index("--query") + 1],
                    {"value": [fixture()[0]["locationIdentity"]]})
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
        self.assertEqual(bound["logAnalyticsCustomerId"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(params["location"], {"value": "eastus2"})
        self.assertEqual(bound["location"], "eastus2")
        self.assertEqual(bound["locationObservations"]["legacyApi"], {"raw": "East US 2", "canonical": "eastus2"})
        self.assertEqual(bound["locationObservations"]["recordedApiParameter"],
                         {"raw": "eastus2", "canonical": "eastus2"})
        self.assertIsNone(baseline)
        self.assertEqual([call.args[0] for call in compiled.call_args_list],
                         ["api.bicep", "api-private-network.bicep"])
        self.assertNotIn("secret-must-not-escape", json.dumps((params, bound, expected)))
        projected = next(args[-1] for args in calls if args[-1].startswith("properties.parameters."))
        for name, definition in api["parameters"].items():
            if definition["type"] == "securestring":
                self.assertNotIn(name + ".value", projected)

    def test_foundation_is_exactly_its_network_resources_not_the_root(self):
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

    def test_full_arm_activation_and_cutover_packets_are_retired(self):
        for packet, phase, app_name in (("activate", "activate", "ca-api-private-test"),
                                        ("cutover", "private", "ca-web-test")):
            with self.subTest(packet=packet):
                bound, _, params = fixture()
                bound.update(packet=packet, phase=phase, apiOrigin="https://ca-api-private-test.example.azurecontainerapps.io")
                prefix = f"/subscriptions/{bound['subscription']}/resourceGroups/{bound['resourceGroup']}/providers/"
                target = prefix + "Microsoft.App/containerApps/" + app_name
                before = {
                    "location": "East US 2", "tags": {},
                    "identity": {"type": "UserAssigned", "userAssignedIdentities": {prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/existing": {}}},
                    "properties": {
                        "managedEnvironmentId": prefix + "Microsoft.App/managedEnvironments/existing",
                        "configuration": {"activeRevisionsMode": "Single", "ingress": {"external": True}, "secrets": []},
                        "template": {"scale": {"minReplicas": 1, "maxReplicas": 1 if packet == "activate" else 3},
                                     "containers": [{"image": "same-retained-image", "env": [
                                         {"name": "AI4IA_PRIVATE_RECOVERY_CANDIDATE", "value": "true"},
                                         {"name": "API_BASE_URL", "value": "https://old.example"},
                                         {"name": "API_PUBLIC_URL", "value": "https://old.example"},
                                     ]}]},
                    },
                }
                after = deepcopy(before)
                after["location"] = "eastus2"
                rows = PREVIEW.env_rows(after["properties"]["template"])
                if packet == "activate":
                    rows["AI4IA_PRIVATE_RECOVERY_CANDIDATE"]["value"] = "false"
                    after["properties"]["template"]["scale"]["maxReplicas"] = 3
                else:
                    for name in ("API_BASE_URL", "API_PUBLIC_URL"):
                        rows[name]["value"] = bound["apiOrigin"]
                raw = {"status": "Succeeded", "changes": [{
                    "resourceId": target, "changeType": "Modify", "before": before, "after": after,
                    "delta": [{"path": "properties.template"}],
                }]}
                with self.assertRaisesRegex(PREVIEW.PreviewError, "Unknown scoped recovery packet"):
                    PREVIEW.scope_preview(raw, bound, params, before)

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

        source, native = deepcopy(baseline), deepcopy(raw)
        before_props = source["properties"]
        after_props = native["changes"][0]["after"]["properties"]
        before_props["template"].update(initContainers=None, serviceBinds=None, terminationGracePeriodSeconds=None, volumes=None)
        before_props["template"]["scale"]["rules"] = None
        before_props["configuration"].update(identitySettings=[], maxInactiveRevisions=None)
        before_props["configuration"]["ingress"]["transport"] = "Auto"
        before_props["template"]["containers"][0]["env"] += [
            {"name": "AI4IA_MODEL_GATEWAY_API_KEY", "secretRef": "model-gateway-api-key"},
            {"name": "AI4IA_HARD_QUOTA_ROLLOUT_ID", "value": ""},
        ]
        after_props["configuration"]["secrets"][0]["name"] = "*******"
        after_props["template"]["containers"][0]["env"] += [
            {"name": "AI4IA_MODEL_GATEWAY_API_KEY", "secretRef": "*******"},
            {"name": "AI4IA_HARD_QUOTA_ROLLOUT_ID"},
        ]
        descriptor = {
            "secrets": [{"name": "model-gateway-api-key", "kind": "containerApp"}],
            "envSecretRefs": {"AI4IA_MODEL_GATEWAY_API_KEY": "model-gateway-api-key"},
            "envValueNames": sorted(row["name"] for row in after_props["template"]["containers"][0]["env"]
                                    if "secretRef" not in row),
            "emptyEnvValues": ["AI4IA_HARD_QUOTA_ROLLOUT_ID"], "valueOrigins": [],
        }
        proven_bound = {**bound, "secretMetadataDigest": PREVIEW.digest(descriptor)}
        proven_params = {**params, "sourceSecretMetadata": descriptor}
        result = PREVIEW.scope_preview(native, proven_bound, proven_params, source)
        self.assertEqual(result["blockingReasons"], [])
        self.assertEqual(result["changes"][0]["secretMetadataProof"], "source-bound; native masked")
        for fault in ("value", "ingress", "registry", "null_setting", "identity_setting"):
            bad_source, changed = deepcopy(source), deepcopy(native)
            target_props = changed["changes"][0]["after"]["properties"]
            if fault == "value":
                PREVIEW.env_rows(target_props["template"])["OTHER_SETTING"]["value"] = "changed"
            elif fault == "ingress":
                target_props["configuration"]["ingress"]["allowInsecure"] = True
            elif fault == "registry":
                target_props["configuration"]["registries"][0]["server"] = "other.azurecr.io"
            elif fault == "null_setting":
                bad_source["properties"]["template"]["initContainers"] = [{"name": "must-retain"}]
            else:
                bad_source["properties"]["configuration"]["identitySettings"] = [{"identity": "must-retain"}]
            with self.subTest(masked_fault=fault):
                self.assertTrue(PREVIEW.scope_preview(changed, proven_bound, proven_params, bad_source)["blockingReasons"])
        wrong_proof = {**proven_bound, "secretMetadataDigest": "0" * 64}
        with self.assertRaises(PREVIEW.PreviewError):
            PREVIEW.scope_preview(native, wrong_proof, proven_params, source)

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
        for fault in ("none", "parameters", "main", "claimed", "unknown-claim", "admission", "nsg-rules", "nsg-etag",
                      "inventory", "inventory-drift", "cutover-inventory", "cutover-inventory-drift"):
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
                inventory = {
                    "appId": f"/subscriptions/{bound['subscription']}/resourceGroups/{bound['resourceGroup']}"
                             "/providers/Microsoft.App/containerApps/ca-api-test",
                    "pageCount": 11, "rowCount": 201, "digest": "a" * 64,
                    "activeRevisions": [], "replicaReadCount": 0,
                }
                if "inventory" in fault:
                    bound.update(packet="cutover" if fault.startswith("cutover") else "activate",
                                 legacyInventory=inventory, imageReuse="ancestor, no image inputs changed",
                                 imageSourceChanges=["scripts/check-api-private-network.py"])
                current_inventory = {**inventory, "digest": "b" * 64} if fault.endswith("-drift") else inventory
                succeeds = fault in ("none", "inventory", "cutover-inventory")
                nsg = {
                    "id": raw["changes"][6]["resourceId"].casefold(), "location": "eastus2", "etag": "initial",
                    "state": "Succeeded", "rulesPresent": True, "rules": [],
                    "subnets": [raw["changes"][0]["resourceId"] + "/subnets/api"],
                }
                preview["nsgComparisons"][0]["liveReadDigest"] = PREVIEW.digest(nsg)
                current_nsg = deepcopy(nsg)
                if fault == "nsg-rules":
                    current_nsg["rules"] = [{}]
                elif fault == "nsg-etag":
                    current_nsg["etag"] = "changed"
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
                     patch.object(PREVIEW, "cloud_read", return_value=current_nsg), \
                     patch.object(PREVIEW.subprocess, "run", return_value=claim), \
                     patch.object(PREVIEW.ADMISSION, "admission",
                                  return_value={"AI4IA_PRIVATE_LEGACY_INVENTORY": json.dumps(current_inventory)},
                                  side_effect=ValueError("blocked") if fault == "admission" else None), \
                     redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
                    self.assertEqual(PREVIEW.main(), 0 if succeeds else 1)
                self.assertEqual(len(writes), 1 if succeeds else 0)
                evidence = (output / "execution.json").read_text() if (output / "execution.json").exists() else ""
                self.assertNotIn("private-value-never-publish", evidence + stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("sensitive-provider-message", stderr.getvalue())
                if succeeds:
                    self.assertEqual(json.loads(evidence)["runtimeAcceptance"], "not_yet_observed")
                    self.assertEqual(len(json.loads(evidence)["nsgReads"]), 1)
                    if "inventory" in fault:
                        context = json.loads(evidence)["context"]
                        self.assertEqual(context["legacyInventory"], inventory)
                        self.assertEqual(context["imageReuse"], "ancestor, no image inputs changed")
                        self.assertEqual(context["imageSourceChanges"], ["scripts/check-api-private-network.py"])
                self.assertFalse((packet / "parameters.json").exists())

    def test_binding_is_same_head_phase_scope_inputs_delta_and_fresh(self):
        bound, raw, params = fixture()
        bound["locationObservations"] = {"legacyApi": {"raw": "East US 2", "canonical": "eastus2"}}
        result = PREVIEW.scope_preview(raw, bound, params)
        token = PREVIEW.make_token(bound, result, 1792860000)
        PREVIEW.verify_token(token, bound, result, 1792860010)
        for field in ("source", "phase", "subscription", "resourceGroup", "parameters", "template",
                      "locationIdentity", "locationObservations"):
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

    def test_global_dns_and_locationless_child_do_not_relax_regional_resources(self):
        bound, raw, params = fixture()
        bound["packet"] = "foundation"
        zone = raw["changes"][3]["resourceId"]
        raw["changes"][3].update(changeType="Create", after={"location": "global", "tags": {}, "properties": {}})
        raw["changes"][2].update(changeType="Create", after={"properties": {
            "privateDnsZoneConfigs": [{"properties": {"privateDnsZoneId": zone}}],
        }})
        result = PREVIEW.scope_preview(raw, bound, params)
        self.assertEqual(result["blockingReasons"], [])
        for wrong in ("East US 2", None, "unknown"):
            changed = deepcopy(raw)
            changed["changes"][3]["after"]["location"] = wrong
            self.assertIn("unapproved_region", PREVIEW.scope_preview(changed, bound, params)["blockingReasons"])
        raw["changes"][2]["after"]["location"] = "unknown"
        with self.assertRaises(ValueError):
            PREVIEW.scope_preview(raw, bound, params)

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
                ], "networkSecurityGroup": {"id": raw["changes"][6]["resourceId"]}}},
                {"name": "cosmos", "properties": {"addressPrefix": "10.241.0.64/27",
                 "networkSecurityGroup": {"id": raw["changes"][7]["resourceId"]},
                 "privateEndpointNetworkPolicies": "Disabled"}},
            ]}})
        self.assertEqual(PREVIEW.scope_preview(raw, bound, params)["blockingReasons"], [])
        display = deepcopy(raw)
        display["changes"][0]["after"]["location"] = "East US 2"
        display_report = PREVIEW.scope_preview(display, bound, params)
        self.assertEqual(display_report["blockingReasons"], [])
        self.assertEqual(display_report["changes"][0]["location"], {"raw": "East US 2", "canonical": "eastus2"})
        for location in ("westus", "West US", "East-US-2", "East  US 2", None, "", True, "global"):
            changed = deepcopy(raw)
            changed["changes"][0]["after"]["location"] = location
            with self.subTest(location=location), self.assertRaises(ValueError):
                PREVIEW.scope_preview(changed, bound, params)
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


class PatchCloud:
    def __init__(self):
        original, raw, _, _, legacy = activation_fixture()
        self.current = deepcopy(raw["changes"][0]["before"])
        self.legacy = legacy
        self.current["properties"]["configuration"]["ingress"]["fqdn"] = "ca-api-private-test.example.azurecontainerapps.io"
        for row in (self.current, self.legacy):
            row["properties"]["provisioningState"] = "Succeeded"
        container = self.current["properties"]["template"]["containers"][0]
        container["env"].extend([
            {"name": "AI4IA_MODEL_GATEWAY_API_KEY", "secretRef": "model-gateway-api-key"},
            {"name": "EMPTY_SETTING"},
        ])
        container["probes"] = [{"type": "Readiness", "httpGet": {"path": "/health/ready", "port": 8000}}]
        container["resources"]["ephemeralStorage"] = "2Gi"
        self.current["properties"]["configuration"]["secrets"] = [{"name": "model-gateway-api-key"}]
        self.image = container["image"]
        self.inventory = {"appId": self.legacy["id"], "pageCount": 1, "rowCount": 1, "digest": "d" * 64,
                          "activeRevisions": [], "replicaReadCount": 0}
        self.revisions = [{"name": self.current["properties"]["latestReadyRevisionName"],
                           "properties": {"active": True, "healthState": "Healthy", "provisioningState": "Provisioned"}}]
        self.calls = []
        self.writes = []
        self.fault = ""
        self.source = original["source"]
        self.admission_calls = 0
        self.app_name = "ca-api-private-test"

    def admission(self, _env):
        self.admission_calls += 1
        if self.admission_calls == 4 and self.fault == "late-config":
            self.current["properties"]["configuration"]["maxInactiveRevisions"] = 99
        if self.admission_calls == 4 and self.fault == "late-legacy":
            self.inventory["digest"] = "e" * 64
        return {"AI4IA_API_PRIVATE_INITIAL_IMAGE": self.image,
                "AI4IA_PRIVATE_API_ORIGIN": "https://ca-api-private-test.example.azurecontainerapps.io",
                "AI4IA_PRIVATE_LEGACY_INVENTORY": json.dumps(self.inventory)}

    def read(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["containerapp", "env", "list-usages"]:
            return {"value": [{"name": {"value": "ManagedEnvironmentConsumptionCores"},
                               "currentValue": 0.5, "limit": 500}]}
        assert args[:3] == ["rest", "--method", "get"]
        url = args[args.index("--url") + 1]
        query = args[args.index("--query") + 1]
        assert "api-version=2024-10-02-preview" in url
        if "/revisions?" in url:
            data = {"value": self.revisions}
        elif "/revisions/" in url:
            name = url.split("/revisions/")[1].split("?")[0]
            data = next(row for row in self.revisions if row["name"] == name)
        else:
            data = self.current if "/ca-api-private-test?" in url else self.legacy
        return deepcopy(jmespath.search(query, data))

    def run(self, args, **kwargs):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return self.source.encode()
        if args[0] == "gh":
            return json.dumps({"object": {"sha": self.source}}).encode()
        if args[:3] == ["az", "acr", "login"]:
            return b""
        assert args[:4] == ["az", "rest", "--method", "patch"]
        assert "--yaml" not in args
        body = json.loads(Path(args[args.index("--body") + 1][1:]).read_bytes())
        assert set(body) == {"properties"} and set(body["properties"]) == {"template"}
        self.writes.append(body)
        if self.fault == "transport":
            raise PREVIEW.PreviewError("Unknown write outcome")
        template = deepcopy(body["properties"]["template"])
        name = self.app_name + "--" + template["revisionSuffix"]
        self.current["properties"].update(template=template, latestRevisionName=name, latestReadyRevisionName=name)
        self.current["properties"]["template"]["containers"][0]["resources"]["ephemeralStorage"] = "2Gi"
        if self.fault == "post-config":
            self.current["properties"]["configuration"]["maxInactiveRevisions"] = 99
        elif self.fault == "post-secret":
            secrets = self.current["properties"]["configuration"].get("secrets")
            if not secrets:
                self.current["properties"]["configuration"]["secrets"] = [{"name": "model-gateway-api-key"}]
            self.current["properties"]["configuration"]["secrets"][0].update(
                keyVaultUrl="https://changed.vault.azure.net/secrets/key",
                identity=next(iter(self.current["identity"]["userAssignedIdentities"])),
            )
        elif self.fault == "post-env":
            self.current["properties"]["template"]["containers"][0]["env"][2]["value"] = "changed"
        elif self.fault == "post-identity":
            key = next(iter(self.current["identity"]["userAssignedIdentities"]))
            self.current["identity"]["userAssignedIdentities"][key] = {"principalId": "changed-principal"}
        elif self.fault == "post-probes":
            probes = self.current["properties"]["template"]["containers"][0]["probes"]
            self.current["properties"]["template"]["containers"][0]["probes"] = (
                [] if probes else [{"type": "Liveness", "httpGet": {"path": "/changed", "port": 3000}}]
            )
        elif self.fault == "post-image":
            self.current["properties"]["template"]["containers"][0]["image"] = "changed"
        elif self.fault == "post-storage":
            self.current["properties"]["template"]["containers"][0]["resources"]["ephemeralStorage"] = "4Gi"
        self.revisions.append({"name": name, "properties": {
            "active": True, "healthState": "Healthy", "provisioningState": "Provisioned",
            "template": deepcopy(self.current["properties"]["template"]),
        }})
        return b""


class WebPatchCloud(PatchCloud):
    def __init__(self, *, voice=True):
        super().__init__()
        self.api = deepcopy(self.current)
        self.api["properties"]["template"]["containers"][0]["env"][0]["value"] = "false"
        self.api["properties"]["template"]["scale"]["maxReplicas"] = 3
        self.app_name = "ca-web-test"
        prefix = self.current["id"].split("/providers/")[0] + "/providers/"
        self.current["id"] = prefix + "Microsoft.App/containerApps/ca-web-test"
        self.current["identity"] = {"type": "UserAssigned", "userAssignedIdentities": {
            prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/id-web-test": {},
        }}
        props = self.current["properties"]
        props.update(latestRevisionName="ca-web-test--ready", latestReadyRevisionName="ca-web-test--ready")
        props["configuration"]["secrets"] = None
        props["configuration"]["ingress"].update(
            fqdn="ca-web-test.example.azurecontainerapps.io",
            customDomains=[{"name": "app.example.test", "certificateId": "retained"}],
        )
        self.legacy["properties"]["configuration"]["ingress"]["fqdn"] = "ca-api-test.example.azurecontainerapps.io"
        self.old_origin = "https://ca-api-test.example.azurecontainerapps.io"
        self.new_origin = "https://ca-api-private-test.example.azurecontainerapps.io"
        self.readiness_calls = 0
        container = props["template"]["containers"][0]
        container["name"] = "web"
        container["image"] = "synthetic.azurecr.io/ai4ia/web-test@sha256:" + "2" * 64
        container["env"] = [
            {"name": "API_BASE_URL", "value": self.old_origin},
            {"name": "OTHER", "value": "retained"},
            {"name": "VOICE_LIVE_ENABLED", "value": "true"},
        ]
        if voice:
            container["env"].append({"name": "API_PUBLIC_URL", "value": self.old_origin})
        container["probes"] = []
        props["template"]["scale"]["maxReplicas"] = 3
        self.image = container["image"]
        self.revisions = [{"name": "ca-web-test--ready", "properties": {
            "active": True, "healthState": "Healthy", "provisioningState": "Provisioned",
        }}]

    def admission(self, env):
        result = super().admission(env)
        result["AI4IA_API_PRIVATE_INITIAL_IMAGE"] = self.api["properties"]["template"]["containers"][0]["image"]
        result["AI4IA_PRIVATE_WEB_IMAGE"] = self.image
        if self.admission_calls == 4 and self.fault == "late-api":
            self.api["properties"]["template"]["containers"][0]["env"][0]["value"] = "true"
        return result

    def ready(self, origin):
        assert origin == self.new_origin
        self.readiness_calls += 1
        return not (self.fault == "late-ready" and self.readiness_calls >= 4)

    def read(self, args, **kwargs):
        if args[:3] == ["rest", "--method", "get"]:
            url = args[args.index("--url") + 1]
            query = args[args.index("--query") + 1]
            if "/ca-api-private-test?" in url:
                self.calls.append(args)
                return deepcopy(jmespath.search(query, self.api))
            if "/ca-web-test?" in url:
                self.calls.append(args)
                return deepcopy(jmespath.search(query, self.current))
        return super().read(args, **kwargs)


class ActivationPatchTests(unittest.TestCase):
    def test_body_is_exact_template_only_and_preserves_image_env_refs_and_probes(self):
        cloud = PatchCloud()
        before = deepcopy(cloud.current)
        template = PREVIEW.activation_patch_template(cloud.current, cloud.legacy)
        token = "1700000000:" + "a" * 64
        body = PREVIEW.activation_patch_body(template, token)
        PREVIEW.validate_activation_patch_body(body, cloud.current, cloud.legacy, token)
        self.assertEqual(cloud.current, before)
        self.assertEqual(body["properties"]["template"]["revisionSuffix"], "activate-" + "a" * 16)
        self.assertEqual(template["containers"][0]["image"], cloud.image)
        self.assertEqual(template["containers"][0]["probes"], before["properties"]["template"]["containers"][0]["probes"])
        self.assertEqual(template["containers"][0]["env"][-2:], before["properties"]["template"]["containers"][0]["env"][-2:])
        self.assertNotIn("ephemeralStorage", template["containers"][0]["resources"])
        for mutate in (
            lambda row: row.update(configuration={}),
            lambda row: row["properties"].update(secrets=[]),
            lambda row: row["properties"]["template"].update(identity={}),
            lambda row: row["properties"]["template"]["containers"][0].update(image="other"),
            lambda row: row["properties"]["template"]["containers"][0]["env"][2].update(value="other"),
            lambda row: row["properties"]["template"]["containers"][0]["env"][-2].update(secretRef="other"),
            lambda row: row["properties"]["template"]["containers"][0]["probes"].clear(),
            lambda row: row["properties"]["template"].update(revisionSuffix="other"),
        ):
            changed = deepcopy(body)
            mutate(changed)
            with self.assertRaises(ValueError):
                PREVIEW.validate_activation_patch_body(changed, cloud.current, cloud.legacy, token)
        for fault in ("flag", "legacy-scale", "cpu"):
            current, legacy = deepcopy(cloud.current), deepcopy(cloud.legacy)
            if fault == "flag":
                current["properties"]["template"]["containers"][0]["env"][0]["value"] = "false"
            elif fault == "legacy-scale":
                legacy["properties"]["template"]["scale"]["maxReplicas"] = 4
            else:
                current["properties"]["template"]["containers"][0]["resources"]["cpu"] = 1.0
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                PREVIEW.activation_patch_template(current, legacy)
        with self.assertRaisesRegex(ValueError, "Forbidden"):
            PREVIEW.activation_patch_body({**template, "configuration": {}}, token)

    def flow(self, fault="", *, web=False, voice=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**ENV, "AI4IA_API_NETWORK_PHASE": "activate", "AI4IA_PRIVATE_STEP": "activate",
                   "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123",
                   "GITHUB_RUN_ATTEMPT": "1",
                   "GITHUB_REPOSITORY": "ian-t-adams/AI4IA", "RUNNER_TEMP": directory,
                   "GITHUB_OUTPUT": str(root / "output"), "GITHUB_ENV": str(root / "env"),
                   "AI4IA_PRIVATE_IMAGE_RECEIPT": "", "AI4IA_RELEASE_KIND": "preview", "AI4IA_RELEASE_PROVISION": "true"}
            cloud = WebPatchCloud(voice=voice) if web else PatchCloud()
            if web:
                env.update(AI4IA_API_NETWORK_PHASE="private", AI4IA_PRIVATE_STEP="cutover",
                           AI4IA_WEB_CUSTOM_DOMAIN="app.example.test", GITHUB_EVENT_NAME="workflow_dispatch",
                           GITHUB_WORKFLOW_REF="ian-t-adams/AI4IA/.github/workflows/deploy.yml@refs/heads/main",
                           GITHUB_WORKFLOW_SHA="a" * 40, GITHUB_SERVER_URL="https://github.com", GITHUB_RUN_ATTEMPT="1")
            env["AI4IA_PRIVATE_IMAGE_RECEIPT"] = json.dumps({"image": cloud.image})
            health = PREVIEW.ROLLOUT.HttpOutcome(status=503 if fault == "post-ready" else 200,
                                                body=b'{"status":"ok","stage":"session_store"}')
            with patch.object(PREVIEW.ADMISSION, "admission", side_effect=cloud.admission), \
                 patch.object(PREVIEW.ADMISSION, "prove_ready",
                              side_effect=cloud.ready if isinstance(cloud, WebPatchCloud) else lambda _: True), \
                 patch.object(PREVIEW.PROVENANCE, "consume_private_api", return_value={"verified": "original-producer"}), \
                 patch.object(PREVIEW, "run", side_effect=cloud.run), \
                 patch.object(PREVIEW.ROLLOUT, "http_request", return_value=health), redirect_stdout(io.StringIO()):
                PREVIEW.activation_patch_command("prepare", env, cloud.read)
                report = json.loads((root / "ai4ia-private-preview/report.json").read_text())
                self.assertFalse(report["preview"]["nativeWhatIf"])
                self.assertFalse(report["executionAuthorized"])
                env.update(AI4IA_RELEASE_KIND="deploy", AI4IA_PRIVATE_PREVIEW_BINDING=report["binding"])
                PREVIEW.activation_patch_command("prepare", env, cloud.read)
                for line in (root / "env").read_text().splitlines():
                    key, value = line.split("=", 1)
                    env[key] = value
                if fault == "pre-config":
                    cloud.current["properties"]["configuration"]["ingress"]["external"] = False
                elif fault == "pre-secret":
                    cloud.current["properties"]["configuration"]["secrets"][0]["name"] = "webiq-api-key"
                elif fault == "legacy":
                    cloud.inventory["activeRevisions"] = ["ca-api-test--active"]
                elif fault == "suffix":
                    cloud.revisions.append({"name": report["revision"], "properties": {"active": False}})
                elif fault == "body":
                    path = Path(env["AI4IA_PRIVATE_PACKET_DIR"]) / "patch.json"
                    body = json.loads(path.read_bytes())
                    body["properties"]["configuration"] = {"secrets": []}
                    path.write_text(json.dumps(body))
                elif fault == "pre-template":
                    cloud.current["properties"]["template"]["containers"][0]["env"][2]["value"] = "changed"
                elif fault == "rerun":
                    env["GITHUB_RUN_ATTEMPT"] = "2"
                cloud.fault = fault
                if fault:
                    with self.assertRaises((ValueError, KeyError)):
                        PREVIEW.activation_patch_command("apply", env, cloud.read)
                else:
                    PREVIEW.activation_patch_command("apply", env, cloud.read)
                after_dispatch = fault in ("transport", "post-config", "post-secret", "post-env",
                                           "post-identity", "post-probes", "post-image", "post-ready", "post-storage") or not fault
                self.assertEqual(len(cloud.writes), 1 if after_dispatch else 0)
                receipt_path = root / "ai4ia-private-preview/execution.json"
                if after_dispatch:
                    receipt = json.loads(receipt_path.read_bytes())
                    self.assertEqual(receipt["status"], "ready_pending_owner_acceptance" if not fault else
                                     "outcome_unknown" if fault == "transport" else "post_check_failed")
                    self.assertNotIn("UNCHANGED_SETTING", json.dumps(receipt))
                    self.assertFalse((Path(env["AI4IA_PRIVATE_PACKET_DIR"]) / "patch.json").exists())
                    if fault == "transport":
                        PREVIEW.activation_patch_command("prepare", env, cloud.read)
                        for line in (root / "env").read_text().splitlines():
                            key, value = line.split("=", 1)
                            env[key] = value
                        with self.assertRaisesRegex(PREVIEW.PreviewError, "already attempted"):
                            PREVIEW.activation_patch_command("apply", env, cloud.read)
                        self.assertEqual(len(cloud.writes), 1)
                if not fault:
                    if web:
                        sent = cloud.writes[0]["properties"]["template"]["containers"][0]["env"]
                        mapped = {row["name"]: row["value"] for row in sent}
                        self.assertEqual(mapped["API_BASE_URL"], cloud.new_origin)
                        self.assertEqual(mapped.get("API_PUBLIC_URL"), cloud.new_origin if voice else None)
                        self.assertEqual(mapped["VOICE_LIVE_ENABLED"], "true")
                    with self.assertRaises(ValueError):
                        PREVIEW.activation_patch_command("apply", env, cloud.read)
                    self.assertEqual(len(cloud.writes), 1)
                    env[PREVIEW.ROLLOUT.DEFAULT_TOKEN_ENV] = "synthetic-owner-token"
                    models = json.loads((ROOT / "infra/models.json").read_text())["catalog"]
                    options = {"models": [{"id": row["name"], "residencies": ["us"]} for row in models
                                          if row["category"] == "decision" and row.get("runtimeEnabled", True)]}
                    with patch.object(PREVIEW.ROLLOUT, "http_request", side_effect=[
                        health, PREVIEW.ROLLOUT.HttpOutcome(status=200, body=json.dumps(options).encode()),
                    ]) as http:
                        owner = PREVIEW.verify_activation_owner(env, receipt, cloud.read)
                        self.assertEqual(owner["ownerOptions"], "passed")
                        self.assertTrue(http.call_args.args[1].endswith("/api/decisions/options"))
                        if web:
                            self.assertEqual(http.call_args.args[1], "https://app.example.test/api/decisions/options")
                        self.assertNotIn("synthetic-owner-token", json.dumps(owner))
                    with patch.object(PREVIEW.ROLLOUT, "http_request", side_effect=[
                        health, PREVIEW.ROLLOUT.HttpOutcome(status=403, body=b"{}"),
                    ]):
                        with self.assertRaises(PREVIEW.PreviewError):
                            PREVIEW.verify_activation_owner(env, receipt, cloud.read)
                    env.pop(PREVIEW.ROLLOUT.DEFAULT_TOKEN_ENV)
                    with patch.object(PREVIEW.ROLLOUT, "http_request") as no_http:
                        with self.assertRaisesRegex(PREVIEW.PreviewError, "token is unavailable"):
                            PREVIEW.verify_activation_owner(env, receipt, cloud.read)
                        no_http.assert_not_called()

    def test_real_patch_dispatch_and_owner_read_only_acceptance(self):
        self.flow()

    def test_preconditions_replay_body_and_postconditions_fail_closed(self):
        for fault in ("pre-config", "pre-secret", "pre-template", "legacy", "late-config", "late-legacy",
                      "suffix", "body", "rerun", "transport", "post-config", "post-secret", "post-env",
                      "post-identity", "post-probes", "post-image", "post-ready"):
            with self.subTest(fault=fault):
                self.flow(fault)

    def test_private_revision_claim_uses_complete_scoped_pages(self):
        cloud = PatchCloud()
        binding = {"subscription": ENV["AZURE_SUBSCRIPTION_ID"], "resourceGroup": "rg-ai4ia-test",
                   "environment": "test", "packet": "activate"}
        url = "https://management.azure.com" + cloud.current["id"] + "/revisions?api-version=2024-10-02-preview&pageSize=20&skipToken=next"
        row = {"name": "ca-api-private-test--first", "active": True, "health": "Healthy", "state": "Provisioned"}
        pages = [{"rows": [row], "nextLink": url}, {"rows": [{**row, "name": "ca-api-private-test--second"}]}]
        calls = []
        def read(args, **kwargs):
            calls.append(args)
            self.assertGreater(kwargs["timeout"], 0)
            return deepcopy(pages[len(calls) - 1])
        self.assertEqual(len(PREVIEW.private_revision_rows(binding, read)), 2)
        self.assertIn("skipToken=next", calls[1][calls[1].index("--url") + 1])
        for link in (url.replace("management.azure.com", "outside.example"), url + "&extra=1"):
            calls.clear()
            pages[0]["nextLink"] = link
            with self.assertRaises(ValueError):
                PREVIEW.private_revision_rows(binding, read)
        pages[0]["nextLink"] = url
        pages[1]["rows"] = [row]
        calls.clear()
        with self.assertRaisesRegex(ValueError, "duplicated"):
            PREVIEW.private_revision_rows(binding, read)

    def test_web_cutover_reuses_guards_and_does_not_add_absent_voice_row(self):
        self.flow(web=True)
        self.flow(web=True, voice=False)
        for fault in ("pre-config", "pre-template", "late-config", "late-legacy", "late-api", "late-ready",
                      "suffix", "body", "rerun", "transport", "post-config", "post-secret",
                      "post-env", "post-identity", "post-probes", "post-image", "post-ready", "post-storage"):
            with self.subTest(fault=fault):
                self.flow(fault, web=True)

    def test_web_template_refuses_wrong_origins_and_preserves_all_other_fields(self):
        cloud = WebPatchCloud()
        before = deepcopy(cloud.current)
        template = PREVIEW.web_cutover_template(cloud.current, cloud.old_origin, cloud.new_origin, "test")
        for origin in ("http://ca-api-private-test.example.azurecontainerapps.io",
                       "https://outside.example", cloud.new_origin + "/extra", cloud.new_origin + "?q=x"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                PREVIEW.web_cutover_template(cloud.current, cloud.old_origin, origin, "test")
        self.assertEqual(cloud.current, before)
        body = PREVIEW.activation_patch_body(template, "1700000000:" + "a" * 64, step="cutover")
        self.assertEqual(body["properties"]["template"]["revisionSuffix"], "cutover-" + "a" * 16)
        for mutate in (
            lambda row: row["properties"].update(configuration={"secrets": []}),
            lambda row: row["properties"]["template"]["containers"][0].update(image="changed"),
            lambda row: row["properties"]["template"]["containers"][0]["env"][1].update(value="changed"),
            lambda row: row["properties"]["template"]["containers"][0]["probes"].append({"type": "Liveness"}),
        ):
            altered = deepcopy(body)
            mutate(altered)
            with self.assertRaises(ValueError):
                PREVIEW.validate_template_patch_body(altered, body)
        original = PREVIEW.activation_template_projection(before["properties"]["template"])
        for value in (template, original):
            for row in value["containers"][0]["env"]:
                if row["name"] in ("API_BASE_URL", "API_PUBLIC_URL"):
                    row["value"] = "comparison-only"
        self.assertEqual(template, original)


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
        cloud.old[0]["active"] = True
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
