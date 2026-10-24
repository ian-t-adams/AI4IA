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
import unittest
from unittest.mock import patch

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
            env = {**ENV, "RUNNER_TEMP": directory, "AI4IA_RELEASE_KIND": "preview",
                   "GITHUB_OUTPUT": str(Path(directory) / "outputs"), "GITHUB_ENV": str(Path(directory) / "env")}
            observed = []
            def command(args, **kwargs):
                self.assertEqual(args[:4], ["az", "deployment", "sub", "what-if"])
                self.assertIn("Provider", args)
                parameters = Path(args[args.index("--parameters") + 1][1:])
                self.assertIn(params["secret"]["value"], parameters.read_text())
                observed.append(parameters)
                return json.dumps(raw).encode()
            console = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "prepare"]), \
                 patch.object(PREVIEW, "compiled_context", return_value=({}, params, bound)), \
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
                 patch.object(PREVIEW, "compiled_context", return_value=({}, params, bound)), \
                 patch.object(PREVIEW, "run", side_effect=command), redirect_stdout(console):
                self.assertEqual(PREVIEW.main(), 0)
            self.assertTrue(Path(env["GITHUB_OUTPUT"]).read_text().endswith("authorized=true\n"))
            self.assertTrue((Path(directory) / "ai4ia-private-preview/proof.json").exists())
            changed = {**bound, "parameters": "d" * 64}
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["preview", "prepare"]), \
                 patch.object(PREVIEW, "compiled_context", return_value=({}, params, changed)), \
                 patch.object(PREVIEW, "run", side_effect=command), redirect_stdout(console):
                self.assertEqual(PREVIEW.main(), 1)
            self.assertTrue(Path(env["GITHUB_OUTPUT"]).read_text().endswith("authorized=false\n"))
            self.assertTrue(all(not path.exists() for path in observed))

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
