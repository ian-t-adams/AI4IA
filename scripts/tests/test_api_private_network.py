"""Offline phase admission and exact, read-only cloud projections."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from scripts._api_private import api_name, validate_prefixes

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

SPEC = importlib.util.spec_from_file_location("private_network_check", ROOT / "scripts/check-api-private-network.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
VERIFY_SPEC = importlib.util.spec_from_file_location("private_postdeploy_test", ROOT / "scripts/post-deploy-verify.py")
assert VERIFY_SPEC and VERIFY_SPEC.loader
VERIFIER = importlib.util.module_from_spec(VERIFY_SPEC)
sys.modules[VERIFY_SPEC.name] = VERIFIER
VERIFY_SPEC.loader.exec_module(VERIFIER)
SUB = "11111111-1111-4111-8111-111111111111"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg-ai4ia-test/providers"
VNET = SCOPE + "/Microsoft.Network/virtualNetworks/vnet-ai4ia-test-api-private"
ZONE = SCOPE + "/Microsoft.Network/privateDnsZones/privatelink.documents.azure.com"
COSMOS = SCOPE + "/Microsoft.DocumentDB/databaseAccounts/cosmos-test"
IDENTITY = SCOPE + "/Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test"
ENV = {
    "AZURE_ENV_NAME": "test", "AZURE_SUBSCRIPTION_ID": SUB,
    "AI4IA_API_NETWORK_PHASE": "candidate", "AI4IA_DEPLOY_VERIFY_CANARY": "false",
    "AI4IA_API_PRIVATE_VNET_PREFIX": "10.241.0.0/25",
    "AI4IA_API_PRIVATE_SUBNET_PREFIX": "10.241.0.0/26",
    "AI4IA_COSMOS_PRIVATE_ENDPOINT_SUBNET_PREFIX": "10.241.0.64/27",
}


def image(service):
    return f"synthetic.azurecr.io/ai4ia/{service}-test@sha256:" + "1" * 64


class Cloud:
    def __init__(self, *, staged=True):
        self.calls = []
        self.apps = {}
        self.details = {}
        for name, service in (("ca-api-private-test", "api"), ("ca-api-test", "api"),
                              ("ca-web-test", "web"), ("ca-proxy-test", "proxy")):
            self.apps[name] = {
                "identity": {"type": "UserAssigned", "userAssignedIdentities": {IDENTITY: {"clientId": "same"}}},
                "properties": {
                    "managedEnvironmentId": SCOPE + "/Microsoft.App/managedEnvironments/cae-ai4ia-test-api-private",
                    "latestReadyRevisionName": name + "--ready", "latestRevisionName": name + "--ready",
                    "configuration": {"ingress": {"fqdn": name + ".synthetic.azurecontainerapps.io"}},
                },
            }
            self.details[name] = {"properties": {
                "active": True, "healthState": "Healthy", "provisioningState": "Provisioned",
                "template": {"containers": [{"image": image(service), "env": [
                    {"name": "AI4IA_COSMOS_ENDPOINT", "value": "https://cosmos-test.documents.azure.com:443/"},
                    {"name": "AI4IA_PRIVATE_RECOVERY_CANDIDATE", "value": str(staged).lower()},
                    {"name": "AI4IA_PRIVATE_COSMOS_REQUIRED", "value": "true"},
                ]}]},
            }}
        self.old = [{"name": "ca-api-test--ready", "properties": {"active": False}}]
        self.details["ca-web-test"]["properties"]["template"]["containers"][0]["env"] = [
            {"name": key, "value": "https://ca-api-test.synthetic.azurecontainerapps.io"}
            for key in ("API_BASE_URL", "API_PUBLIC_URL")
        ]
        self.replicas = []
        self.networks = []
        self.network = {"properties": {"vnetConfiguration": {"infrastructureSubnetId": VNET + "/subnets/api"}}}
        self.cosmos = {"id": COSMOS, "publicNetworkAccess": "Disabled", "disableLocalAuth": True}
        self.endpoint = {"privateLinkServiceConnections": [{
            "privateLinkServiceId": COSMOS, "groupIds": ["Sql"],
            "privateLinkServiceConnectionState": {"status": "Approved"},
        }]}
        self.groups = [{"privateDnsZoneConfigs": [{"privateDnsZoneId": ZONE}]}]
        self.links = [{"virtualNetwork": {"id": VNET}, "registrationEnabled": False,
                       "virtualNetworkLinkState": "Completed"}]

    def read(self, args, *, absent=False):
        self.calls.append(args)
        assert args[args.index("--subscription") + 1] == SUB
        if args[:3] == ["network", "vnet", "list"]:
            return copy.deepcopy(self.networks)
        assert args[args.index("-g") + 1] == "rg-ai4ia-test"
        if args[:2] == ["containerapp", "show"]:
            return copy.deepcopy(self.apps[args[args.index("-n") + 1]])
        if args[:3] == ["containerapp", "revision", "show"]:
            return copy.deepcopy(self.details[args[args.index("-n") + 1]])
        routes = [
            (["containerapp", "revision", "list"], self.old),
            (["containerapp", "replica", "list"], self.replicas),
            (["containerapp", "env", "show"], self.network),
            (["cosmosdb", "show"], self.cosmos),
            (["network", "private-endpoint", "show"], self.endpoint),
            (["network", "private-endpoint", "dns-zone-group", "list"], self.groups),
            (["network", "private-dns", "link", "vnet", "list"], self.links),
        ]
        for prefix, result in routes:
            if args[:len(prefix)] == prefix:
                return copy.deepcopy(result)
        raise AssertionError(f"Unexpected command: {args[:4]}")


class PrivateAdmissionTests(unittest.TestCase):
    def test_each_phase_binds_target_preserves_images_and_reads_runtime(self):
        for selected, staged in (("candidate", True), ("activate", True), ("private", False)):
            with self.subTest(selected=selected):
                cloud = Cloud(staged=staged)
                urls = []
                def ready(url):
                    urls.append(url)
                    return True
                result = MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": selected}, cloud.read, ready)
                self.assertEqual(result, {
                    "AZURE_API_APP_NAME": "ca-api-private-test",
                    "AI4IA_API_PRIVATE_INITIAL_IMAGE": image("api"),
                    "AI4IA_PRIVATE_WEB_IMAGE": image("web"),
                    "AI4IA_PRIVATE_PROXY_IMAGE": image("proxy"),
                })
                self.assertEqual(urls, ["https://ca-api-private-test.synthetic.azurecontainerapps.io"])
                self.assertLessEqual(len(cloud.calls), 24)

    def test_first_candidate_is_inert_and_cannot_skip_to_activation(self):
        cloud = Cloud()
        cloud.apps["ca-api-private-test"] = None
        self.assertEqual(MODULE.admission(ENV, cloud.read)["AI4IA_API_PRIVATE_INITIAL_IMAGE"], MODULE.PLACEHOLDER)
        for phase in ("activate", "private"):
            with self.assertRaises(ValueError):
                MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": phase}, cloud.read)

    def test_staging_and_live_worker_guards_are_load_bearing(self):
        cloud = Cloud()
        with self.assertRaisesRegex(ValueError, "Activate and verify"):
            MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": "private"}, cloud.read, lambda _: True)
        for phase in ("activate", "private"):
            cloud = Cloud(staged=phase == "activate")
            cloud.old[0]["properties"]["active"] = True
            with self.assertRaisesRegex(ValueError, "drained and inactive"):
                MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": phase}, cloud.read, lambda _: True)
        with self.assertRaisesRegex(ValueError, "demote"):
            MODULE.admission(ENV, Cloud(staged=False).read, lambda _: True)
        cloud = Cloud()
        cloud.replicas = ["terminating"]
        with self.assertRaisesRegex(ValueError, "drain is incomplete"):
            MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": "activate"}, cloud.read, lambda _: True)

    def test_interrupted_first_stage_can_replace_only_its_inert_placeholder(self):
        cloud = Cloud()
        cloud.apps["ca-api-private-test"]["properties"]["latestReadyRevisionName"] = None
        cloud.details["ca-api-private-test"]["properties"]["healthState"] = "Unhealthy"
        cloud.details["ca-api-private-test"]["properties"]["template"]["containers"][0]["image"] = MODULE.PLACEHOLDER
        result = MODULE.admission(ENV, cloud.read, lambda _: self.fail("Placeholder is not runtime proof"))
        self.assertEqual(result["AI4IA_API_PRIVATE_INITIAL_IMAGE"], MODULE.PLACEHOLDER)
        for selected in ("activate", "private"):
            with self.assertRaisesRegex(ValueError, "placeholder"):
                MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": selected}, cloud.read, lambda _: True)

    def test_incomplete_or_changed_evidence_cannot_authorize(self):
        def container(cloud):
            return cloud.details["ca-api-private-test"]["properties"]["template"]["containers"][0]
        mutations = [
            lambda c: c.cosmos.update(publicNetworkAccess="Enabled"),
            lambda c: c.cosmos.update(disableLocalAuth=False),
            lambda c: c.network["properties"]["vnetConfiguration"].update(infrastructureSubnetId="wrong"),
            lambda c: c.endpoint["privateLinkServiceConnections"][0].update(groupIds=["MongoDB"]),
            lambda c: c.endpoint["privateLinkServiceConnections"][0]["privateLinkServiceConnectionState"].update(status="Pending"),
            lambda c: c.groups.clear(),
            lambda c: c.links[0].update(registrationEnabled=True),
            lambda c: c.apps["ca-api-private-test"]["properties"].update(latestRevisionName="pending"),
            lambda c: c.apps["ca-api-private-test"]["identity"].update(type="SystemAssigned"),
            lambda c: container(c).update(image="synthetic.azurecr.io/ai4ia/api-test:latest"),
            lambda c: container(c)["env"][0].update(value="https://another.documents.azure.com"),
            lambda c: container(c)["env"][2].update(value="false"),
            lambda c: container(c)["env"][1].update(value="unknown"),
            lambda c: c.networks.append({"id": "other", "prefixes": ["10.241.0.0/16"], "peerings": []}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                cloud = Cloud()
                mutate(cloud)
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    MODULE.admission(ENV, cloud.read, lambda _: True)
        with self.assertRaisesRegex(ValueError, "runtime"):
            MODULE.admission(ENV, Cloud().read, lambda _: False)
        with self.assertRaisesRegex(ValueError, "no-model-call"):
            MODULE.admission({**ENV, "AI4IA_DEPLOY_VERIFY_CANARY": ""}, Cloud().read, lambda _: True)

    def test_legacy_cannot_silently_retarget_after_private_staging(self):
        env = {"AZURE_ENV_NAME": "test", "AZURE_SUBSCRIPTION_ID": SUB}
        cloud = Cloud()
        with self.assertRaisesRegex(ValueError, "implicit return"):
            MODULE.admission(env, cloud.read)
        cloud.apps["ca-api-private-test"] = None
        self.assertEqual(MODULE.admission(env, cloud.read), {"AZURE_API_APP_NAME": "ca-api-test"})

    def test_completed_cutover_cannot_regress_to_activation_or_candidate(self):
        cloud = Cloud(staged=False)
        for row in cloud.details["ca-web-test"]["properties"]["template"]["containers"][0]["env"]:
            row["value"] = "https://ca-api-private-test.synthetic.azurecontainerapps.io"
        self.assertEqual(MODULE.admission(
            {**ENV, "AI4IA_API_NETWORK_PHASE": "private"}, cloud.read, lambda _: True,
        )["AZURE_API_APP_NAME"], "ca-api-private-test")
        self.assertEqual(MODULE.admission(
            {**ENV, "AI4IA_API_NETWORK_PHASE": "private", "AI4IA_DEPLOY_VERIFY_CANARY": "true"},
            cloud.read, lambda _: True,
        )["AZURE_API_APP_NAME"], "ca-api-private-test")
        for phase in ("activate", "candidate"):
            with self.assertRaisesRegex(ValueError, "undo a completed cutover"):
                MODULE.admission({**ENV, "AI4IA_API_NETWORK_PHASE": phase}, cloud.read, lambda _: True)

    def test_cidr_and_name_contracts(self):
        validate_prefixes("candidate", "10.241.0.0/25", "10.241.0.0/26", "10.241.0.64/27")
        validate_prefixes("legacy", "", "", "")
        for values in (
            ("private", "", "", ""),
            ("candidate", "10.241.0.0/25", "10.241.0.0/26", "10.241.0.32/27"),
            ("candidate", "172.30.0.0/25", "172.30.0.0/26", "172.30.0.64/27"),
            ("candidate", "20.1.0.0/25", "20.1.0.0/26", "20.1.0.64/27"),
            ("legacy", "10.241.0.0/25", "", ""),
        ):
            with self.assertRaises(ValueError):
                validate_prefixes(*values)
        self.assertEqual(api_name("test", "private"), "ca-api-private-test")
        with self.assertRaises(ValueError):
            api_name("test\nINJECT=value", "private")

    def test_read_failure_is_not_absence_and_errors_do_not_leak(self):
        for error, absent in ((b"ERROR: (ResourceNotFound) absent", True),
                              (b"ERROR: (Forbidden) ResourceNotFound in detail", False)):
            result = subprocess.CompletedProcess([], 1, b"", error)
            with patch.object(MODULE.subprocess, "run", return_value=result):
                if absent:
                    self.assertIsNone(MODULE.cli(["containerapp", "show"], absent=True))
                else:
                    with self.assertRaisesRegex(ValueError, "metadata read failed"):
                        MODULE.cli(["containerapp", "show"], absent=True)

    def test_workflow_and_hooks_use_gate_before_capture_or_parameter_resolution(self):
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertLess(workflow.index("run: python scripts/check-api-private-network.py"),
                        workflow.index("- name: Capture pre-provision revisions"))
        hooks = (ROOT / "azure.yaml").read_text()
        self.assertIn("resourceName: ${AZURE_API_APP_NAME}", hooks)
        self.assertEqual(hooks.count("scripts/check-api-private-network.py --azd-env"), 2)
        self.assertIn('recovery+=(--decision-options)', workflow)
        self.assertIn('--skip-canary "${recovery[@]}"', workflow)
        self.assertEqual(workflow.count(
            'if [ "${AI4IA_DEPLOY_VERIFY_CANARY:-}" = "false" ] && [ "${AI4IA_API_NETWORK_PHASE:-legacy}" = "legacy" ]; then'
        ), 2)
        for name in ("AI4IA_API_PRIVATE_INITIAL_IMAGE", "AI4IA_PRIVATE_WEB_IMAGE", "AI4IA_PRIVATE_PROXY_IMAGE"):
            self.assertIn(f"      {name}: ''", workflow)
            self.assertNotIn(f"vars.{name}", workflow)


class NonbillableOptionsTests(unittest.TestCase):
    def test_real_postdeploy_canary_branch_requires_auth_and_never_calls_a_model(self):
        args = VERIFIER.build_parser().parse_args([
            "verify", "--state", "unused.json", "--skip-canary", "--decision-options",
        ])
        catalog = json.loads((ROOT / "infra/models.json").read_text())
        model_id = next(row["name"] for row in catalog["catalog"] if row["category"] == "decision")
        payload = {"models": [{"id": model_id, "displayName": "Decision", "residencies": ["US"]}]}
        with patch.dict(os.environ, {**ENV, args.token_env: "synthetic-token"}), \
             patch.object(VERIFIER, "emit"), \
             patch.object(VERIFIER, "run_canary", side_effect=AssertionError("No model/session work allowed")), \
             patch.object(VERIFIER, "http_request") as http:
            http.return_value = VERIFIER.HttpOutcome(status=200, body=json.dumps(payload).encode())
            self.assertEqual(VERIFIER._canary_failures(args, "https://candidate.test"), [])
            self.assertEqual(http.call_args.args, ("GET", "https://candidate.test/api/decisions/options"))
            self.assertEqual(http.call_args.kwargs["headers"], {"Authorization": "Bearer synthetic-token"})
            self.assertEqual(http.call_args.kwargs["body_limit"], 8192)
            for status, document in ((503, payload), (403, payload), (401, payload), (200, {"models": []}),
                                     (200, {"models": [{"id": "unknown"}]}), (200, {"notModels": []})):
                with self.subTest(status=status, document=document):
                    http.return_value = VERIFIER.HttpOutcome(status=status, body=json.dumps(document).encode())
                    self.assertTrue(VERIFIER._canary_failures(args, "https://candidate.test"))
            http.reset_mock()
            os.environ[args.token_env] = ""
            self.assertTrue(VERIFIER._canary_failures(args, "https://candidate.test"))
            http.assert_not_called()
            args.skip_canary = False
            self.assertTrue(VERIFIER._canary_failures(args, "https://candidate.test"))
            http.assert_not_called()
            args.skip_canary = True
            os.environ[args.token_env] = "synthetic-token"
            os.environ["AI4IA_API_NETWORK_PHASE"] = "legacy"
            self.assertTrue(VERIFIER._canary_failures(args, "https://candidate.test"))
            http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
