from __future__ import annotations

import importlib.util
import html
import io
import json
import shutil
import subprocess
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
        expected, expected_catalog_fragments = (
            gateway_generator.generate_endpoint_policies()
        )
        output = (
            ROOT / "infra/policies/simplel7proxy-endpoints.xml"
        ).read_text(encoding="utf-8")
        self.assertEqual(output, expected)
        gateway_generator.validate_policy_fragment(
            output, "infra/policies/simplel7proxy-endpoints.xml"
        )
        for path, catalog_expected in zip(
            gateway_generator.CATALOG_OUTPUT_PATHS,
            expected_catalog_fragments,
            strict=True,
        ):
            catalog_output = path.read_text(encoding="utf-8")
            self.assertEqual(catalog_output, catalog_expected)
            gateway_generator.validate_policy_fragment(
                catalog_output, str(path.relative_to(ROOT))
            )
        ElementTree.parse(ROOT / "infra/policies/simplel7proxy-priority-retry.xml")
        gateway_generator.validate_policy_expressions(
            (
                ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
            ).read_text(encoding="utf-8"),
            "infra/policies/simplel7proxy-priority-retry.xml",
        )
        priority_expected, priority_fragments = (
            gateway_generator.generate_priority_policies()
        )
        priority_output = gateway_generator.PRIORITY_OUTPUT_PATH.read_text(
            encoding="utf-8"
        )
        self.assertEqual(priority_output, priority_expected)
        gateway_generator.validate_policy_expressions(
            priority_output,
            str(gateway_generator.PRIORITY_OUTPUT_PATH.relative_to(ROOT)),
        )
        self.assertLessEqual(
            len(priority_output.encode("utf-8")),
            gateway_generator.APIM_API_POLICY_MAX_BYTES,
        )
        rollback_policy = (
            ROOT / "infra/policies/simplel7proxy-rollback-policy.xml"
        ).read_text(encoding="utf-8")
        ElementTree.fromstring(rollback_policy)
        gateway_generator.validate_policy_expressions(
            rollback_policy,
            "infra/policies/simplel7proxy-rollback-policy.xml",
        )
        self.assertLessEqual(
            len(rollback_policy.encode("utf-8")),
            gateway_generator.APIM_API_POLICY_MAX_BYTES,
        )
        for path, fragment_expected in zip(
            gateway_generator.PRIORITY_OUTPUT_PATHS,
            priority_fragments,
            strict=True,
        ):
            fragment_output = path.read_text(encoding="utf-8")
            self.assertEqual(fragment_output, fragment_expected)
            gateway_generator.validate_policy_fragment(
                fragment_output,
                str(path.relative_to(ROOT)),
            )
            self.assertNotIn("<base", fragment_output)
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
        gateway_generator.validate_realtime_policy(
            realtime_output, "infra/policies/realtime-routing.xml"
        )
        legacy_realtime = ROOT / "infra/policies/realtime-routing-legacy.xml"
        ElementTree.parse(legacy_realtime)
        self.assertIn("<set-body>", legacy_realtime.read_text(encoding="utf-8"))

    def test_catalog_expressions_stay_below_apim_limit(self) -> None:
        output, catalog_fragments = gateway_generator.generate_endpoint_policies()
        roots = [
            ElementTree.fromstring(fragment)
            for fragment in (*catalog_fragments, output)
        ]
        catalog_expressions = [
            element.attrib["value"]
            for root in roots
            for element in root.findall("set-variable")
            if element.attrib.get("name", "").startswith("backendCatalog")
        ]
        self.assertEqual(
            len(catalog_expressions),
            gateway_generator.CATALOG_FRAGMENT_COUNT + 1,
        )
        self.assertTrue(
            all(
                len(expression) < gateway_generator.APIM_EXPRESSION_MAX_CHARS
                for expression in catalog_expressions
            )
        )

    def test_every_generated_fragment_stays_below_compiler_ceiling(self) -> None:
        setup_fragment, catalog_fragments = (
            gateway_generator.generate_endpoint_policies()
        )
        for fragment in (*catalog_fragments, setup_fragment):
            self.assertLessEqual(
                len(fragment.encode("utf-8")),
                gateway_generator.APIM_FRAGMENT_COMPILER_SAFE_BYTES,
            )
            self.assertLessEqual(
                len(html.unescape(fragment).encode("utf-8")),
                gateway_generator.APIM_FRAGMENT_COMPILER_SAFE_BYTES,
            )

    def test_priority_splitter_rejects_lossy_inbound_order(self) -> None:
        source = (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
        ).read_text(encoding="utf-8")
        cases = {
            "base policy must be the first": source.replace(
                "<inbound>\n\t\t<base />",
                '<inbound>\n\t\t<set-header name="before-base" '
                'exists-action="override"><value>x</value></set-header>\n'
                "\t\t<base />",
                1,
            ),
            "fragment chain must be contiguous": source.replace(
                '<include-fragment fragment-id="endpoint_selection_catalog_1_32" />',
                '<include-fragment fragment-id="endpoint_selection_catalog_1_32" />'
                '\n\t\t<set-header name="interleaved" exists-action="override">'
                "<value>x</value></set-header>",
                1,
            ),
        }
        for expected_error, malformed in cases.items():
            with self.subTest(expected_error=expected_error):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "priority.xml"
                    path.write_text(malformed, encoding="utf-8")
                    with (
                        patch.object(
                            gateway_generator,
                            "PRIORITY_POLICY_PATH",
                            path,
                        ),
                        self.assertRaisesRegex(ValueError, expected_error),
                    ):
                        gateway_generator.generate_priority_policies()

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

    def test_policy_validator_rejects_jobject_index_initializers(self) -> None:
        policy = (
            '<fragment><set-variable name="unsupported" '
            'value="@{ return new JObject { [&quot;key&quot;] = 1 }; }" />'
            "</fragment>"
        )
        with self.assertRaisesRegex(ValueError, "JObject index initializers"):
            gateway_generator.validate_policy_fragment(
                policy, "unsupported-initializer.xml"
            )

    def test_fragment_validator_rejects_literal_uri_tokens(self) -> None:
        policy = (
            '<fragment><set-variable name="unsupported" '
            'value="@{ return &quot;https://example.com&quot;; }" />'
            "</fragment>"
        )
        with self.assertRaisesRegex(ValueError, "literal '://'"):
            gateway_generator.validate_policy_fragment(
                policy, "unsupported-uri.xml"
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
        fragment = "\n".join(
            path.read_text(encoding="utf-8")
            for path in gateway_generator.CATALOG_OUTPUT_PATHS
        )
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
                    f"{{{{foundry-{deployment['region']}-realtime-wss-endpoint}}}}/openai/realtime",
                    policy,
                )

    def test_topology_is_proxy_then_apim_then_foundry(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        self.assertIn("host=${apimV2.properties.gatewayUrl};mode=apim", gateway)
        self.assertIn("output proxyIngressUrl string = '${proxyUrl}/openai'", gateway)
        self.assertIn("serviceUrl: foundryOpenAiUrl", gateway)
        self.assertNotIn("serviceUrl: '${proxyUrl}", gateway)
        self.assertNotIn("foundryBackends[0]", gateway)
        self.assertIn(
            "loadTextContent('../policies/realtime-routing.xml')", gateway
        )
        self.assertIn(
            "loadTextContent('../policies/simplel7proxy-priority-policy.xml')",
            gateway,
        )
        self.assertIn("uniqueString(definition.value)", gateway)
        self.assertIn("var modelApiPolicyValue = reduce(", gateway)
        self.assertNotIn("simplel7proxy-rollback-policy.xml", gateway)
        for path in gateway_generator.PRIORITY_OUTPUT_PATHS:
            self.assertIn(
                f"loadTextContent('../policies/{path.name}')",
                gateway,
            )
        for index, path in enumerate(gateway_generator.CATALOG_OUTPUT_PATHS):
            self.assertIn(
                f"loadTextContent('../policies/{path.name}')",
                gateway,
            )
            self.assertIn(
                f'fragment-id="{gateway_generator.CATALOG_FRAGMENT_IDS[index]}"',
                (
                    ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
                ).read_text(encoding="utf-8"),
            )
        policy = (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
        ).read_text(encoding="utf-8")
        ordered_ids = [
            *gateway_generator.CATALOG_FRAGMENT_IDS,
            gateway_generator.SETUP_FRAGMENT_ID,
        ]
        positions = [
            policy.index(f'fragment-id="{fragment_id}"')
            for fragment_id in ordered_ids
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("endpoint_selection_setup_31", policy)
        self.assertNotIn("endpoint_selection_catalog_0_31", policy)
        self.assertNotIn('fragment-id="endpoint_selection_frag_30"', policy)
        self.assertNotIn("name: 'endpoint_selection_frag_30'", gateway)
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

    def test_basic_v2_replacement_retains_consumption_rollback_and_rewires_callers(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        api = (ROOT / "infra/modules/api.bicep").read_text(encoding="utf-8")

        # The declared Consumption service and every legacy child remain the rollback
        # plane. Active caller expressions refer only to the replacement.
        self.assertIn("name: take('apim-${workload}-${environmentName}', 50)", gateway)
        self.assertIn("name: 'Consumption'", gateway)
        for legacy_child in (
            "foundryEndpointValues", "modelPolicyFragments", "modelsApi",
            "modelOperations", "modelsApiPolicy", "proxyModelSubscription",
            "realtimeApi", "realtimeOperation", "realtimeApiPolicy",
            "apiRealtimeSubscription", "apimOpenAiUsers", "apimCognitiveUsers",
        ):
            self.assertIn(f"resource {legacy_child} ", gateway)

        self.assertIn("name: take('apim-v2-${workload}-${environmentName}', 50)", gateway)
        self.assertIn("name: 'BasicV2'", gateway)
        self.assertIn("capacity: 1", gateway)
        self.assertIn("loadTextContent('../policies/realtime-routing-legacy.xml')", gateway)
        self.assertIn("resource replacementFoundryEndpointValues", gateway)
        self.assertIn("resource replacementModelPolicyFragments", gateway)
        self.assertIn("resource replacementModelsApi", gateway)
        self.assertIn("resource replacementModelOperations", gateway)
        self.assertIn("resource replacementModelsApiPolicy", gateway)
        self.assertIn("resource replacementProxyModelSubscription", gateway)
        self.assertIn("resource replacementApimDiagnostics", gateway)
        self.assertIn("resource replacementApimOpenAiUsers", gateway)
        self.assertIn("resource replacementApimCognitiveUsers", gateway)
        self.assertIn("value: replacementProxyModelSubscription.listSecrets().primaryKey", gateway)
        self.assertIn("value: replacementProxyIngressSubscription.listSecrets().primaryKey", gateway)
        self.assertIn("modelGatewayUrl: gateway.outputs.proxyIngressUrl", main)
        self.assertIn("modelGatewayApiKey: gateway.outputs.proxyIngressKey", main)
        self.assertIn("realtimeGatewayApiKey: gateway.outputs.realtimeGatewayKey", main)
        self.assertIn("#disable-next-line no-unnecessary-dependson\n    gateway", main)
        self.assertIn("replacementModelsApiPolicy", gateway.split("resource proxyApp", 1)[1])

        # A product with no API association produces the opaque proxy ingress key.
        self.assertIn("resource replacementProxyIngressProduct", gateway)
        self.assertIn("scope: '/products/ai4ia-proxy-ingress'", gateway)
        self.assertNotIn("replacementProxyIngressProductApi", gateway)
        self.assertIn("AI4IA_REALTIME_GATEWAY_API_KEY", api)
        self.assertIn("realtime-gateway-api-key", api)

    def test_realtime_replacement_is_a_websocket_api_with_supported_policy(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        policy = (ROOT / "infra/policies/realtime-routing.xml").read_text(encoding="utf-8")
        self.assertIn("resource replacementRealtimeApi", gateway)
        replacement = gateway.split("resource replacementRealtimeApi ", 1)[1].split(
            "resource replacementRealtimeApiPolicy", 1
        )[0]
        self.assertIn("apiType: 'websocket'", replacement)
        self.assertIn("'wss'", replacement)
        self.assertIn("serviceUrl: primaryFoundryRealtimeWssUrl", replacement)
        self.assertNotIn("resource replacementRealtimeOperation", gateway)
        self.assertIn("replacementRealtimeWssEndpointValues", gateway)
        self.assertIn("replace(endsWith(backend.endpoint, '/')", gateway)
        self.assertIn("'https://', 'wss://'", gateway)
        self.assertIn("/openai/realtime", policy)
        self.assertIn("-realtime-wss-endpoint", policy)
        self.assertNotIn("<set-body>", policy)
        self.assertIn("<set-status code=\"404\"", policy)
        self.assertIn("<set-backend-service", policy)
        self.assertIn("<authentication-managed-identity", policy)
        gateway_generator.validate_realtime_policy(policy, "realtime-routing.xml")

    def test_compiled_arm_declares_both_services_and_active_replacement_shape(self) -> None:
        gateway_path = ROOT / "infra/modules/gateway.bicep"
        bicep = shutil.which("bicep")
        if bicep:
            command = [bicep, "build", str(gateway_path), "--stdout"]
        else:
            az = shutil.which("az")
            if not az:
                self.skipTest("Bicep CLI and Azure CLI are unavailable")
            command = [az, "bicep", "build", "--file", str(gateway_path), "--stdout", "--only-show-errors"]
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8"
        )
        template = json.loads(completed.stdout.lstrip("\ufeff"))
        resources = template["resources"]
        self.assertEqual(resources["apim"]["sku"], {"name": "Consumption", "capacity": 0})
        self.assertEqual(resources["apimV2"]["sku"], {"name": "BasicV2", "capacity": 1})
        self.assertEqual(resources["apimV2"]["identity"]["type"], "SystemAssigned")
        realtime = resources["replacementRealtimeApi"]["properties"]
        self.assertEqual(realtime["apiType"], "websocket")
        self.assertEqual(realtime["protocols"], ["wss"])
        self.assertIn("primaryFoundryRealtimeWssUrl", json.dumps(realtime["serviceUrl"]))
        self.assertIn("wss://", json.dumps(template["variables"]["primaryFoundryRealtimeWssUrl"]))
        self.assertNotIn("replacementRealtimeOperation", resources)
        self.assertIn("replacementModelsApiPolicy", resources["replacementProxyModelSubscription"]["dependsOn"])
        self.assertIn("replacementRealtimeApiPolicy", resources["replacementApiRealtimeSubscription"]["dependsOn"])
        self.assertIn("replacementApimOpenAiUsers", resources["proxyApp"]["dependsOn"])
        self.assertIn("replacementApimCognitiveUsers", resources["proxyApp"]["dependsOn"])
        self.assertIn("replacementApimDiagnostics", resources)
        resource_types = {resource["type"].lower() for resource in resources.values()}
        self.assertFalse(any(resource_type.endswith("/delete") for resource_type in resource_types))

    def test_compiled_arm_creates_fragments_before_api_policy(self) -> None:
        gateway_path = ROOT / "infra/modules/gateway.bicep"
        bicep = shutil.which("bicep")
        if bicep:
            command = [bicep, "build", str(gateway_path), "--stdout"]
        else:
            az = shutil.which("az")
            if not az:
                self.skipTest("Bicep CLI and Azure CLI are unavailable")
            command = [
                az,
                "bicep",
                "build",
                "--file",
                str(gateway_path),
                "--stdout",
                "--only-show-errors",
            ]
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        template = json.loads(completed.stdout.lstrip("\ufeff"))
        resources = template["resources"]
        self.assertIn(
            "modelPolicyFragments",
            resources["modelsApiPolicy"]["dependsOn"],
        )
        definitions = [
            *template["variables"]["endpointSelectionFragmentDefinitions"],
            *template["variables"]["priorityPolicyFragmentDefinitions"],
        ]
        self.assertEqual(
            [
                *gateway_generator.CATALOG_FRAGMENT_IDS,
                gateway_generator.SETUP_FRAGMENT_ID,
                *gateway_generator.PRIORITY_FRAGMENT_IDS,
            ],
            [definition["baseName"] for definition in definitions],
        )
        fragment_resource = resources["modelPolicyFragments"]
        self.assertIn("uniqueString", fragment_resource["name"])
        self.assertIn(
            "modelApiPolicyValue",
            resources["modelsApiPolicy"]["properties"]["value"],
        )

    def test_live_compiler_harness_has_exact_name_cleanup_guards(self) -> None:
        harness = (
            ROOT / "scripts/test-apim-policy-compiler.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[guid]::NewGuid()", harness)
        self.assertIn("Assert-DiagnosticName", harness)
        self.assertIn("'If-Match' = '*'", harness)
        self.assertIn("finally {", harness)
        self.assertIn("CLEANUP_VERIFIED_ABSENT_API=", harness)
        self.assertIn("CLEANUP_VERIFIED_ABSENT_FRAGMENT=", harness)
        self.assertNotIn("azd provision", harness)
        self.assertNotIn("az deployment", harness)
        for fragment_id in (
            *gateway_generator.CATALOG_FRAGMENT_IDS,
            gateway_generator.SETUP_FRAGMENT_ID,
            *gateway_generator.PRIORITY_FRAGMENT_IDS,
        ):
            self.assertIn(fragment_id, harness)

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
