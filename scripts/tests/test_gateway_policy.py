from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_generator = load_script(
    "gen_gateway_policy", "scripts/gen-gateway-policy.py"
)
feature_validator = load_script(
    "validate_feature_prereqs", "scripts/validate-feature-prereqs.py"
)
docs_generator = load_script("gen_docs_catalog", "scripts/gen-docs-catalog.py")


class GatewayPolicyTests(unittest.TestCase):
    def test_generated_fragment_is_current_and_well_formed(self) -> None:
        expected = gateway_generator.generate()
        output = (
            ROOT / "infra/policies/simplel7proxy-endpoints.xml"
        ).read_text(encoding="utf-8")
        self.assertEqual(output, expected)
        ElementTree.fromstring(output)
        gateway_generator.validate_policy_expressions(
            output, "infra/policies/simplel7proxy-endpoints.xml"
        )
        ElementTree.parse(ROOT / "infra/policies/simplel7proxy-priority-retry.xml")
        gateway_generator.validate_policy_expressions(
            (
                ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
            ).read_text(encoding="utf-8"),
            "infra/policies/simplel7proxy-priority-retry.xml",
        )
        realtime_models = json.loads(
            (ROOT / "infra/models.json").read_text(encoding="utf-8")
        )
        realtime_expected = gateway_generator.generate_realtime_policy(realtime_models)
        realtime_output = (
            ROOT / "infra/policies/realtime-routing.xml"
        ).read_text(encoding="utf-8")
        self.assertEqual(realtime_output, realtime_expected)
        ElementTree.fromstring(realtime_output)
        gateway_generator.validate_policy_expressions(
            realtime_output, "infra/policies/realtime-routing.xml"
        )

    def test_catalog_expressions_stay_below_apim_limit(self) -> None:
        output = gateway_generator.generate()
        root = ElementTree.fromstring(output)
        catalog_expressions = [
            element.attrib["value"]
            for element in root.findall("set-variable")
            if element.attrib.get("name", "").startswith("backendCatalog")
        ]
        self.assertGreater(len(catalog_expressions), 2)
        self.assertTrue(
            all(
                len(expression) < gateway_generator.APIM_EXPRESSION_MAX_CHARS
                for expression in catalog_expressions
            )
        )

    def test_policy_validator_rejects_unterminated_csharp_string(self) -> None:
        malformed = (
            '<fragment><set-variable name="broken" '
            'value="@(&quot;unterminated)" /></fragment>'
        )
        ElementTree.fromstring(malformed)
        with self.assertRaisesRegex(ValueError, "unterminated string literal"):
            gateway_generator.validate_policy_expressions(
                malformed, "malformed-policy.xml"
            )

    def test_policy_validator_rejects_oversized_expression(self) -> None:
        oversized = (
            '<fragment><set-variable name="oversized" value="@(&quot;'
            + ("x" * gateway_generator.APIM_EXPRESSION_MAX_CHARS)
            + '&quot;)" /></fragment>'
        )
        ElementTree.fromstring(oversized)
        with self.assertRaisesRegex(ValueError, "APIM requires"):
            gateway_generator.validate_policy_expressions(
                oversized, "oversized-policy.xml"
            )

    def test_policy_validator_checks_interpolation_delimiters(self) -> None:
        malformed_expressions = (
            '@{ return $&quot;broken {context.RequestId&quot;; }',
            '@{ return $&quot;broken {context.RequestId)&quot;; }',
        )
        for expression in malformed_expressions:
            with self.subTest(expression=expression):
                malformed = (
                    '<fragment><set-variable name="broken" value="'
                    + expression
                    + '" /></fragment>'
                )
                ElementTree.fromstring(malformed)
                with self.assertRaisesRegex(
                    ValueError, "unmatched|unterminated"
                ):
                    gateway_generator.validate_policy_expressions(
                        malformed, "malformed-interpolation.xml"
                    )

    def test_every_catalog_deployment_is_allowlisted(self) -> None:
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        fragment = (
            ROOT / "infra/policies/simplel7proxy-endpoints.xml"
        ).read_text(encoding="utf-8")
        naming = models["naming"]
        for model in models["catalog"]:
            for deployment in model["deployments"]:
                name = gateway_generator.deployment_name(
                    model=model["name"],
                    subscription_token=naming["subscriptionToken"],
                    region=deployment["region"],
                    sku=deployment["sku"],
                    sku_short=naming["skuShort"],
                )
                self.assertIn(name, fragment)
                self.assertIn(
                    f"{{{{foundry-{deployment['region']}-endpoint}}}}", fragment
                )

    def test_retry_contract_and_regional_rewrite_are_present(self) -> None:
        policy = (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
        ).read_text(encoding="utf-8")
        self.assertIn('name="S7PREQUEUE"', policy)
        self.assertIn('name="retry-after-ms"', policy)
        self.assertIn('name="backendRelativePath"', policy)
        self.assertIn('name="selectedDeployment"', policy)
        self.assertIn("model_not_allowed", policy)
        self.assertIn("model_path_mismatch", policy)
        fragment = (
            ROOT / "infra/policies/simplel7proxy-endpoints.xml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "catalog[&quot;DEFAULT&quot;] as JObject ?? new JObject()",
            fragment,
        )

    def test_every_realtime_deployment_has_a_catalog_route(self) -> None:
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        policy = (ROOT / "infra/policies/realtime-routing.xml").read_text(
            encoding="utf-8"
        )
        naming = models["naming"]
        for model in models["catalog"]:
            if model["category"] != "realtime":
                continue
            for deployment in model["deployments"]:
                name = gateway_generator.deployment_name(
                    model=model["name"],
                    subscription_token=naming["subscriptionToken"],
                    region=deployment["region"],
                    sku=deployment["sku"],
                    sku_short=naming["skuShort"],
                )
                self.assertIn(name, policy)
                self.assertIn(
                    f"{{{{foundry-{deployment['region']}-endpoint}}}}/openai/realtime",
                    policy,
                )

    def test_topology_is_proxy_then_apim_then_foundry(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        self.assertIn("host=${apim.properties.gatewayUrl};mode=apim", gateway)
        self.assertIn("output modelGatewayUrl string = '${proxyUrl}/openai'", gateway)
        self.assertIn("serviceUrl: foundryOpenAiUrl", gateway)
        self.assertNotIn("serviceUrl: '${proxyUrl}", gateway)
        self.assertNotIn("foundryBackends[0]", gateway)
        self.assertIn(
            "loadTextContent('../policies/realtime-routing.xml')", gateway
        )
        for probe_path in ("/startup", "/liveness", "/readiness"):
            self.assertIn(f"path: '{probe_path}'", gateway)
        self.assertEqual(gateway.count("port: 8080"), 3)
        self.assertNotIn("port: 9000", gateway)
        self.assertIn("endsWith(backend.endpoint, '/')", gateway)
        self.assertIn("realtimeBaseUrl: gateway.outputs.realtimeGatewayUrl", main)
        self.assertIn("dataPlanePrincipalIds: nativeFoundryPrincipalIds", main)
        self.assertNotIn("proxyIdentity.principalId\n]", main.split(
            "var nativeFoundryPrincipalIds =", 1
        )[1].split("]", 1)[0])

    def test_governance_features_default_off_and_fail_closed(self) -> None:
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        parameters = json.loads(
            (ROOT / "infra/main.parameters.json").read_text(encoding="utf-8")
        )["parameters"]
        for name in (
            "proxyProfilesEnabled",
            "proxyPrioritiesEnabled",
            "proxyEventHubTelemetryEnabled",
            "proxyAsyncEnabled",
        ):
            self.assertIn(f"param {name} bool = false", main)
            self.assertEqual(parameters[name]["value"].rsplit("=", 1)[-1], "false}")
        self.assertIn("@secure()\n@description('Minimal server-owned", main)
        self.assertIn("UserConfigUrl', value: 'file:/mnt/ai4ia-profiles/", gateway)
        self.assertIn("LogAllRequestHeaders', value: 'false'", gateway)
        self.assertIn("LogAllResponseHeaders', value: 'false'", gateway)

    def test_async_iac_follows_diagnostics_and_private_data_posture(self) -> None:
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        async_module = (ROOT / "infra/modules/proxyasync.bicep").read_text(
            encoding="utf-8"
        )
        network = (ROOT / "infra/modules/network.bicep").read_text(encoding="utf-8")
        self.assertGreaterEqual(async_module.count("diagnosticSettings:"), 2)
        self.assertGreaterEqual(
            async_module.count("publicNetworkAccess: publicNetworkAccess"), 2
        )
        self.assertIn("name: 'proxyasyncblob'", main)
        self.assertIn("name: 'proxyasyncbus'", main)
        self.assertIn("serviceBusDnsZoneId", network)
        self.assertIn(
            "proxyEventHubTelemetryEnabled ? [\n  proxyIdentity.principalId", main
        )

    def test_proxy_pin_is_consistent(self) -> None:
        pin = "d9eb1d1fa42820792a9699bfc253562fba07d977"
        self.assertIn(pin, (ROOT / "proxy/README.md").read_text(encoding="utf-8"))
        self.assertIn(pin, (ROOT / "proxy/Dockerfile").read_text(encoding="utf-8"))

    def test_warm_config_changes_never_log_values(self) -> None:
        factory = (
            ROOT / "proxy/SimpleL7Proxy/Config/ConfigFactory.cs"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[WARM-V2]", factory)

    def test_docs_posture_understands_parameter_placeholder_defaults(self) -> None:
        errors: list[str] = []
        docs_generator.check_meta_posture(errors)
        self.assertEqual([], errors)


class FeaturePrerequisiteTests(unittest.TestCase):
    def run_validator(self, parameters: dict[str, object]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "parameters.json"
            path.write_text(
                json.dumps(
                    {
                        "parameters": {
                            name: {"value": value}
                            for name, value in parameters.items()
                        }
                    }
                ),
                encoding="utf-8",
            )
            original = feature_validator.PARAMETERS_FILE
            feature_validator.PARAMETERS_FILE = path
            output = io.StringIO()
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    result = feature_validator.main()
            finally:
                feature_validator.PARAMETERS_FILE = original
            return result, output.getvalue()

    def test_profiles_reject_shared_key_prerequisite(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "proxyProfilesEnabled": True,
                "proxyProfileProjectionJson": '[{"appId":"app-a"}]',
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("verified identity-aware application header", output)

    def test_priorities_require_worker_reservations(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "proxyPrioritiesEnabled": True,
                "proxyPriorityWorkers": "invalid",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("priority:count format", output)

    def test_environment_overrides_parameter_placeholder_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AI4IA_PROXY_PROFILES_ENABLED": "true",
                "AI4IA_PROXY_PROFILE_PROJECTION_JSON": '[{"appId":"app-a"}]',
            },
            clear=False,
        ):
            result, output = self.run_validator(
                {
                    "owner": "operator",
                    "apimPublisherEmail": "ops@contoso.test",
                    "proxyProfilesEnabled": "${AI4IA_PROXY_PROFILES_ENABLED=false}",
                    "proxyProfileProjectionJson": "${AI4IA_PROXY_PROFILE_PROJECTION_JSON=}",
                }
            )
        self.assertEqual(result, 1)
        self.assertIn("verified identity-aware application header", output)

    def test_private_data_tier_requires_vnet_isolation(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "dataTierPrivate": True,
                "vnetIsolationEnabled": False,
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("requires vnetIsolationEnabled=true", output)


if __name__ == "__main__":
    unittest.main()
