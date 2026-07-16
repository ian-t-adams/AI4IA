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
    def test_policy_fragments_normalize_crlf_before_hashing_and_storage(
        self,
    ) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")

        self.assertIn(
            "var normalizedModelPolicyFragmentDefinitions = [for definition in modelPolicyFragmentDefinitions:",
            gateway,
        )
        self.assertIn(
            r"value: replace(definition.value, '\r\n', '\n')",
            gateway,
        )
        self.assertIn(
            "reduce(\n  normalizedModelPolicyFragmentDefinitions,",
            gateway,
        )
        self.assertEqual(
            2,
            gateway.count(
                "for definition in normalizedModelPolicyFragmentDefinitions"
            ),
        )
        self.assertNotIn(
            "reduce(\n  modelPolicyFragmentDefinitions,",
            gateway,
        )
        self.assertNotIn(
            "for definition in modelPolicyFragmentDefinitions: {\n  parent:",
            gateway,
        )

        lf_value = "<fragment>\n  <set-header />\n</fragment>\n"
        crlf_value = lf_value.replace("\n", "\r\n")
        self.assertEqual(lf_value, lf_value.replace("\r\n", "\n"))
        self.assertEqual(lf_value, crlf_value.replace("\r\n", "\n"))

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
        self.assertIn("host=${sharedApimGatewayUrl};mode=apim", gateway)
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

    def test_shared_basic_v2_apim_retains_consumption_rollback_and_rewires_callers(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        apimcore = (ROOT / "infra/modules/apimcore.bicep").read_text(encoding="utf-8")
        mcp = (ROOT / "infra/modules/mcpgateway.bicep").read_text(encoding="utf-8")
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        api = (ROOT / "infra/modules/api.bicep").read_text(encoding="utf-8")

        # Consumption and every original child stay as the inactive rollback plane.
        self.assertIn("name: take('apim-${workload}-${environmentName}', 50)", gateway)
        self.assertIn("name: 'Consumption'", gateway)
        for legacy_child in (
            "foundryEndpointValues", "modelPolicyFragments", "modelsApi",
            "modelOperations", "modelsApiPolicy", "proxyModelSubscription",
            "realtimeApi", "realtimeOperation", "realtimeApiPolicy",
            "apiRealtimeSubscription", "apimOpenAiUsers", "apimCognitiveUsers",
        ):
            self.assertIn(f"resource {legacy_child} ", gateway)

        # gateway creates no BasicV2 service; it consumes apimcore's shared contract.
        self.assertIn("param sharedApimName string", gateway)
        self.assertIn("param sharedApimResourceId string", gateway)
        self.assertIn("param sharedApimGatewayUrl string", gateway)
        self.assertIn("param sharedApimPrincipalId string", gateway)
        self.assertIn("resource sharedApim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing", gateway)
        self.assertNotIn("name: 'BasicV2'", gateway)
        self.assertNotIn("resource sharedApimDiagnostics", gateway)
        for shared_child in (
            "sharedFoundryEndpointValues", "sharedModelPolicyFragments",
            "sharedModelsApi", "sharedModelOperations", "sharedModelsApiPolicy",
            "sharedProxyModelSubscription", "sharedRealtimeApi",
            "sharedRealtimeApiPolicy", "sharedApiRealtimeSubscription",
            "sharedApimOpenAiUsers", "sharedApimCognitiveUsers",
        ):
            self.assertIn(f"resource {shared_child}", gateway)
        self.assertIn("principalId: sharedApimPrincipalId", gateway)
        self.assertIn("guid(foundryAccounts[i].id, sharedApimResourceId", gateway)
        self.assertIn("host=${sharedApimGatewayUrl};mode=apim", gateway)
        self.assertIn("value: sharedProxyModelSubscription.listSecrets().primaryKey", gateway)
        self.assertIn("value: sharedProxyIngressSubscription.listSecrets().primaryKey", gateway)

        # The shared service is unconditional; official MCP children are conditional.
        self.assertIn("module apimcore 'modules/apimcore.bicep'", main)
        self.assertIn("sharedApimName: apimcore.outputs.apimName", main)
        self.assertIn("sharedApimResourceId: apimcore.outputs.apimId", main)
        self.assertIn("sharedApimGatewayUrl: apimcore.outputs.gatewayUrl", main)
        self.assertIn("sharedApimPrincipalId: apimcore.outputs.principalId", main)
        self.assertIn("module mcpgateway 'modules/mcpgateway.bicep' = if (enableOfficialMcp)", main)
        self.assertIn("apimName: apimcore.outputs.apimName", main)
        self.assertIn("gatewayBaseUrl: apimcore.outputs.gatewayUrl", main)
        self.assertIn("[apimcore.outputs.principalId]", main)

        self.assertIn("name: take('apim-mcp-${workload}-${environmentName}', 50)", apimcore)
        self.assertIn("name: 'BasicV2'", apimcore)
        self.assertIn("resource apimDiagnostics", apimcore)

        self.assertIn("param apimName string", mcp)
        self.assertIn("resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing", mcp)
        self.assertNotIn("param enableOfficialMcp", mcp)
        self.assertNotIn("resource apimDiagnostics", mcp)
        self.assertNotIn("name: 'BasicV2'", mcp)
        self.assertNotIn("take('apim-mcp-${workload}-${environmentName}', 50)", mcp)
        self.assertIn("resource mcpProduct", mcp)
        self.assertIn("resource mcpProductApis", mcp)
        self.assertIn("scope: '/products/ai4ia-mcp'", apimcore)
        self.assertNotIn("scope: '/products/ai4ia-mcp'", mcp)
        self.assertNotIn("scope: '/apis'", mcp)
        self.assertIn("output mcpGatewayBaseUrl string = gatewayBaseUrl", mcp)
        self.assertIn(
            "officialMcpSubscriptionKey: enableOfficialMcp ? apimcore.outputs.mcpSubscriptionKey : ''",
            main,
        )

        self.assertIn("modelGatewayUrl: gateway.outputs.proxyIngressUrl", main)
        self.assertIn("modelGatewayApiKey: gateway.outputs.proxyIngressKey", main)
        self.assertIn("realtimeGatewayApiKey: gateway.outputs.realtimeGatewayKey", main)
        self.assertIn("#disable-next-line no-unnecessary-dependson\n    gateway", main)
        # Product has no API association, making this ingress credential opaque.
        self.assertIn("resource sharedProxyIngressProduct", gateway)
        self.assertIn("scope: '/products/ai4ia-proxy-ingress'", gateway)
        self.assertNotIn("sharedProxyIngressProductApi", gateway)
        self.assertIn("AI4IA_REALTIME_GATEWAY_API_KEY", api)
        self.assertIn("realtime-gateway-api-key", api)
        self.assertIn("modelGatewayApiKeyHeader: 'S7P-KEY'", main)
        self.assertIn("header=S7P-KEY", gateway)
        strip_headers = gateway.split("name: 'StripRequestHeaders'", 1)[1].split("]", 1)[0]
        self.assertIn("'S7P-KEY'", strip_headers)
        self.assertNotIn("'Ocp-Apim-Subscription-Key'", strip_headers)

    def test_realtime_shared_api_is_a_websocket_api_with_supported_policy(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        policy = (ROOT / "infra/policies/realtime-routing.xml").read_text(encoding="utf-8")
        self.assertIn("resource sharedRealtimeApi", gateway)
        replacement = gateway.split("resource sharedRealtimeApi ", 1)[1].split(
            "resource sharedRealtimeApiPolicy", 1
        )[0]
        self.assertIn("type: 'websocket'", replacement)
        self.assertIn("'wss'", replacement)
        self.assertIn("serviceUrl: primaryFoundryRealtimeWssUrl", replacement)
        self.assertNotIn("resource sharedRealtimeOperation", gateway)
        self.assertIn("resource sharedRealtimeHandshake", gateway)
        self.assertIn("name: 'onHandshake'", gateway)
        self.assertIn(
            "Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01",
            gateway,
        )
        self.assertIn("sharedRealtimeWssEndpointValues", gateway)
        self.assertIn("replace(endsWith(backend.endpoint, '/')", gateway)
        self.assertIn("'https://', 'wss://'", gateway)
        self.assertIn("/openai/realtime", policy)
        self.assertIn("-realtime-wss-endpoint", policy)
        self.assertNotIn("<set-body>", policy)
        self.assertIn("<set-status code=\"404\"", policy)
        self.assertIn("<set-backend-service", policy)
        self.assertIn("<authentication-managed-identity", policy)
        gateway_generator.validate_realtime_policy(policy, "realtime-routing.xml")

    def test_speech_voice_live_shared_api_is_a_websocket_api_with_supported_policy(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        policy = (ROOT / "infra/policies/speech-voice-live.xml").read_text(encoding="utf-8")
        self.assertIn("resource sharedSpeechVoiceLiveApi", gateway)
        replacement = gateway.split("resource sharedSpeechVoiceLiveApi ", 1)[1].split(
            "resource sharedSpeechVoiceLiveHandshake", 1
        )[0]
        self.assertIn("type: 'websocket'", replacement)
        self.assertIn("'wss'", replacement)
        self.assertIn("path: 'speech/voice-live/realtime'", replacement)
        self.assertIn("serviceUrl: '${speechVoiceLiveWssBase}/voice-live/realtime'", replacement)
        self.assertIn("resource sharedSpeechVoiceLiveHandshake", gateway)
        self.assertIn("name: 'onHandshake'", gateway)
        self.assertIn("resource speechVoiceLiveWssEndpointValue", gateway)
        self.assertIn("resource speechVoiceLiveAudienceValue", gateway)
        self.assertIn("resource sharedSpeechVoiceLiveSubscription", gateway)
        self.assertIn("name: 'ai4ia-api-speech-voice-live'", gateway)
        for query_name in (
            "deployment",
            "subscription-key",
            "api-key",
            "agent_id",
            "project_id",
        ):
            self.assertIn(
                f'<set-query-parameter name="{query_name}" exists-action="delete" />',
                policy,
            )
        for header_name in (
            "Ocp-Apim-Subscription-Key",
            "api-key",
            "Authorization",
        ):
            self.assertIn(
                f'<set-header name="{header_name}" exists-action="delete" />',
                policy,
            )
        gateway_generator.validate_policy_expressions(policy, "speech-voice-live.xml")
        gateway_generator.validate_speech_voice_live_policy(policy, "speech-voice-live.xml")

    def test_speech_voice_live_policy_rejects_missing_selector_or_credential_strip(
        self,
    ) -> None:
        policy = (ROOT / "infra/policies/speech-voice-live.xml").read_text(
            encoding="utf-8"
        )
        cases = (
            '<set-query-parameter name="subscription-key" exists-action="delete" />',
            '<set-query-parameter name="agent_id" exists-action="delete" />',
            '<set-header name="api-key" exists-action="delete" />',
        )
        for required_strip in cases:
            with self.subTest(required_strip=required_strip):
                with self.assertRaisesRegex(ValueError, "must strip caller"):
                    gateway_generator.validate_speech_voice_live_policy(
                        policy.replace(required_strip, "", 1),
                        "speech-voice-live.xml",
                    )

    def test_speech_voice_live_policy_rejects_strip_after_managed_identity(
        self,
    ) -> None:
        policy = (ROOT / "infra/policies/speech-voice-live.xml").read_text(
            encoding="utf-8"
        )
        strip = '<set-query-parameter name="api-key" exists-action="delete" />'
        identity = (
            '<authentication-managed-identity '
            'resource="{{speech-voice-live-mi-audience}}" />'
        )
        reordered = policy.replace(strip, "", 1).replace(
            identity,
            f"{identity}\n    {strip}",
            1,
        )
        with self.assertRaisesRegex(
            ValueError,
            "before managed-identity authentication",
        ):
            gateway_generator.validate_speech_voice_live_policy(
                reordered,
                "speech-voice-live.xml",
            )

    def test_speech_voice_live_policy_rejects_extra_non_inbound_backend_or_identity(
        self,
    ) -> None:
        policy = (ROOT / "infra/policies/speech-voice-live.xml").read_text(
            encoding="utf-8"
        )
        additions = (
            '<set-backend-service base-url="wss://other.example/realtime" />',
            '<authentication-managed-identity resource="https://other.example" />',
        )
        for addition in additions:
            with self.subTest(addition=addition):
                mutated = policy.replace("<backend>", f"<backend>\n    {addition}", 1)
                with self.assertRaisesRegex(ValueError, "expected exactly one"):
                    gateway_generator.validate_speech_voice_live_policy(
                        mutated,
                        "speech-voice-live.xml",
                    )

    def test_speech_voice_live_is_additive_and_isolated_from_other_gateway_planes(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        api = (ROOT / "infra/modules/api.bicep").read_text(encoding="utf-8")

        # Every pre-existing plane (legacy Consumption rollback, the shared
        # active /openai + /openai/realtime APIs, and their subscriptions) is
        # untouched: still present and unrenamed.
        for untouched in (
            "resource legacyConsumptionApim",
            "resource realtimeApi", "resource apiRealtimeSubscription",
            "resource sharedModelsApi", "resource sharedProxyModelSubscription",
            "resource sharedProxyIngressSubscription", "resource sharedRealtimeApi",
            "resource sharedApiRealtimeSubscription",
        ):
            self.assertIn(untouched, gateway)

        # The new API/operation/policy/subscription/named values are their own,
        # distinctly named resources -- not edits to any of the above.
        for new_resource in (
            "resource speechVoiceLiveWssEndpointValue",
            "resource speechVoiceLiveAudienceValue",
            "resource sharedSpeechVoiceLiveApi",
            "resource sharedSpeechVoiceLiveHandshake",
            "resource sharedSpeechVoiceLiveApiPolicy",
            "resource sharedSpeechVoiceLiveSubscription",
            "resource speechVoiceLiveAccount",
            "resource sharedApimSpeechVoiceLiveFoundryUser",
        ):
            self.assertIn(new_resource, gateway)
            declaration = gateway.split(new_resource, 1)[1].split("{", 1)[0]
            self.assertIn(
                "if (speechVoiceLiveEnabled)",
                declaration,
                f"{new_resource} must remain default-off",
            )

        # Distinct subscription scope/name from every other gateway credential.
        self.assertIn("scope: sharedSpeechVoiceLiveApi.id", gateway)
        self.assertIn("name: 'ai4ia-api-speech-voice-live'", gateway)

        # Cognitive Services User remains supplied by the existing account loop.
        # The additional Foundry User grant is scoped to ONE selected account,
        # never the foundryBackends loop, and has a disambiguated guid seed.
        self.assertIn("resource sharedApimCognitiveUsers", gateway)
        self.assertIn(
            "var cognitiveUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'",
            gateway,
        )
        cognitive_rbac = gateway.split("resource sharedApimCognitiveUsers ", 1)[1].split(
            "resource speechVoiceLiveAccount", 1
        )[0]
        self.assertIn("for (backend, i) in foundryBackends", cognitive_rbac)
        self.assertIn("scope: foundryAccounts[i]", cognitive_rbac)
        self.assertIn(
            "roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveUserRoleId)",
            cognitive_rbac,
        )
        self.assertIn(
            "var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'",
            gateway,
        )
        speech_rbac = gateway.split(
            "resource sharedApimSpeechVoiceLiveFoundryUser ", 1
        )[1]
        self.assertIn("scope: speechVoiceLiveAccount", speech_rbac)
        self.assertNotIn("for (backend, i) in foundryBackends", speech_rbac.split("}", 1)[0])
        self.assertIn(
            "guid(speechVoiceLiveAccount.id, sharedApimResourceId, foundryUserRoleId, 'speech-voice-live')",
            gateway,
        )

        # main.bicep wires the dedicated account (reused, not created) and
        # gateway outputs (never a repo/user-suppliable secret) to the api.
        self.assertIn("speechVoiceLiveAccountName: speechVoiceLiveAccountName", main)
        self.assertIn("speechVoiceLiveAccountEndpoint: speechVoiceLiveAccountEndpoint", main)
        self.assertIn("speechVoiceLiveEnabled: speechVoiceLiveEnabled", main)
        self.assertIn(
            "var speechVoiceLiveAccountName = foundry[speechVoiceLiveIndex].outputs.accountName",
            main,
        )
        self.assertIn(
            "var speechVoiceLiveAccountEndpoint = foundry[speechVoiceLiveIndex].outputs.endpoint",
            main,
        )
        self.assertIn("speechVoiceLiveBaseUrl: gateway.outputs.speechVoiceLiveGatewayUrl", main)
        self.assertIn("speechVoiceLiveGatewayApiKey: gateway.outputs.speechVoiceLiveGatewayKey", main)
        self.assertIn("var speechVoiceLiveRegionName = 'eastus2'", main)

        # api.bicep never lets the Speech key flow anywhere but its own env var,
        # and never emits it unless BOTH the master gate and the feature flag
        # are on.
        self.assertIn("AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY", api)
        self.assertIn("AI4IA_SPEECH_VOICE_LIVE_BASE_URL", api)
        self.assertIn("AI4IA_VOICE_PROVIDER_ALLOWLIST", api)
        self.assertIn("AI4IA_VOICE_DEFAULT_PROVIDER", api)
        self.assertIn(
            "var hasSpeechVoiceLiveGatewayKey = realtimeEnabled && speechVoiceLiveEnabled && !empty(speechVoiceLiveGatewayApiKey)",
            api,
        )
        self.assertIn("speech-voice-live-gateway-api-key", api)
        # web.bicep never receives any Speech Voice Live setting: the browser
        # only ever talks to the FastAPI relay, never APIM, for voice.
        web = (ROOT / "infra/modules/web.bicep").read_text(encoding="utf-8")
        self.assertNotIn("speechVoiceLive", web)
        self.assertNotIn("SpeechVoiceLive", web)
        self.assertNotIn("SPEECH_VOICE_LIVE", web)

        policy = (ROOT / "infra/policies/speech-voice-live.xml").read_text(encoding="utf-8")
        self.assertIn(
            '<set-backend-service base-url="{{speech-voice-live-wss-endpoint}}/voice-live/realtime" />',
            policy,
        )
        self.assertIn('<value>gpt-realtime</value>', policy)
        self.assertIn('<value>2026-04-10</value>', policy)
        self.assertIn(
            '<set-query-parameter name="deployment" exists-action="delete" />',
            policy,
        )
        self.assertNotIn("/openai", policy)
        self.assertNotIn("proxy-ingress", policy)
        self.assertNotIn("mcp", policy.lower())
        self.assertNotIn("consumption", policy.lower())

    def _build_bicep_template(self, path: Path) -> dict[str, object]:
        bicep = shutil.which("bicep")
        if bicep:
            command = [bicep, "build", str(path), "--stdout"]
        else:
            az = shutil.which("az")
            if not az:
                self.skipTest("Bicep CLI and Azure CLI are unavailable")
            command = [
                az,
                "bicep",
                "build",
                "--file",
                str(path),
                "--stdout",
                "--only-show-errors",
            ]
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        return json.loads(completed.stdout.lstrip("﻿"))

    @staticmethod
    def _collect_resources(template: dict[str, object]) -> list[dict[str, object]]:
        collected: list[dict[str, object]] = []
        resources = template.get("resources", {})
        values = resources.values() if isinstance(resources, dict) else resources
        for resource in values:
            if not isinstance(resource, dict):
                continue
            collected.append(resource)
            properties = resource.get("properties", {})
            nested = properties.get("template") if isinstance(properties, dict) else None
            if isinstance(nested, dict):
                collected.extend(GatewayPolicyTests._collect_resources(nested))
        return collected

    def test_compiled_arm_reuses_single_shared_basic_v2_apim_and_keeps_consumption_rollback(self) -> None:
        template = self._build_bicep_template(ROOT / "infra/main.bicep")
        all_resources = self._collect_resources(template)
        service_resources = [
            resource for resource in all_resources
            if resource.get("type", "").lower() == "microsoft.apimanagement/service"
            and not resource.get("existing")
        ]
        self.assertEqual(2, len(service_resources))
        serialized = json.dumps(template)
        self.assertNotIn("apim-v2-", serialized)
        self.assertNotIn("apim-rt-", serialized)
        legacy = next(resource for resource in service_resources if resource["sku"]["name"] == "Consumption")
        shared = next(resource for resource in service_resources if resource["sku"]["name"] == "BasicV2")
        self.assertEqual({"name": "Consumption", "capacity": 0}, legacy["sku"])
        self.assertEqual({"name": "BasicV2", "capacity": 1}, shared["sku"])
        self.assertEqual("SystemAssigned", shared["identity"]["type"])
        self.assertIn("apim-mcp-", json.dumps(shared["name"]))

        gateway_template = template["resources"]["gateway"]["properties"]["template"]
        self.assertIn("mcpgateway", template["resources"]["gateway"]["dependsOn"])
        gateway_resources = gateway_template["resources"]
        self.assertIn("sharedApim", gateway_resources)
        self.assertTrue(gateway_resources["sharedApim"].get("existing"))
        self.assertNotIn("sharedApimDiagnostics", gateway_resources)
        realtime = gateway_resources["sharedRealtimeApi"]["properties"]
        self.assertEqual("websocket", realtime["type"])
        self.assertEqual(["wss"], realtime["protocols"])
        self.assertIn("primaryFoundryRealtimeWssUrl", json.dumps(realtime["serviceUrl"]))
        self.assertNotIn("sharedRealtimeOperation", gateway_resources)
        self.assertTrue(gateway_resources["sharedRealtimeHandshake"].get("existing"))
        self.assertEqual(
            "Microsoft.ApiManagement/service/apis/operations/policies",
            gateway_resources["sharedRealtimeApiPolicy"]["type"],
        )
        self.assertIn(
            "onHandshake",
            json.dumps(gateway_resources["sharedRealtimeApiPolicy"]["name"]),
        )
        self.assertIn("sharedModelsApiPolicy", gateway_resources["sharedProxyModelSubscription"]["dependsOn"])
        self.assertIn("sharedRealtimeApiPolicy", gateway_resources["sharedApiRealtimeSubscription"]["dependsOn"])
        self.assertIn("sharedApimOpenAiUsers", gateway_resources["proxyApp"]["dependsOn"])
        self.assertIn("sharedApimCognitiveUsers", gateway_resources["proxyApp"]["dependsOn"])
        self.assertIn("parameters('sharedApimName')", json.dumps(gateway_resources["sharedModelsApi"]))
        self.assertIn("parameters('sharedApimPrincipalId')", json.dumps(gateway_resources["sharedApimOpenAiUsers"]))
        self.assertIn("parameters('sharedApimGatewayUrl')", json.dumps(gateway_template["variables"]["hostEnv"]))

        speech_api = gateway_resources["sharedSpeechVoiceLiveApi"]["properties"]
        self.assertEqual("websocket", speech_api["type"])
        self.assertEqual(["wss"], speech_api["protocols"])
        self.assertEqual("speech/voice-live/realtime", speech_api["path"])
        self.assertNotEqual(gateway_resources["sharedSpeechVoiceLiveApi"]["name"], gateway_resources["sharedRealtimeApi"]["name"])
        self.assertTrue(gateway_resources["sharedSpeechVoiceLiveHandshake"].get("existing"))
        self.assertEqual(
            "Microsoft.ApiManagement/service/apis/operations/policies",
            gateway_resources["sharedSpeechVoiceLiveApiPolicy"]["type"],
        )
        self.assertIn(
            "onHandshake",
            json.dumps(gateway_resources["sharedSpeechVoiceLiveApiPolicy"]["name"]),
        )
        speech_subscription = gateway_resources["sharedSpeechVoiceLiveSubscription"]["properties"]
        self.assertIn("ai4ia-api-speech-voice-live", json.dumps(gateway_resources["sharedSpeechVoiceLiveSubscription"]["name"]))
        self.assertNotEqual(
            speech_subscription["scope"],
            gateway_resources["sharedApiRealtimeSubscription"]["properties"]["scope"],
        )
        speech_rbac = gateway_resources[
            "sharedApimSpeechVoiceLiveFoundryUser"
        ]["properties"]
        self.assertEqual("ServicePrincipal", speech_rbac["principalType"])
        self.assertIn("parameters('sharedApimPrincipalId')", json.dumps(speech_rbac))
        # Voice Live 2026-04-10 requires both roles. The existing account loop
        # supplies Cognitive Services User; the scalar assignment supplies
        # Foundry User (formerly Azure AI User) only to the selected account.
        self.assertEqual(
            "a97b65f3-24c7-4388-baec-2e87135dc908",
            gateway_template["variables"]["cognitiveUserRoleId"],
        )
        self.assertEqual(
            "53ca6127-db72-4b80-b1b0-d745d6d5456d",
            gateway_template["variables"]["foundryUserRoleId"],
        )
        self.assertFalse(
            gateway_template["parameters"]["speechVoiceLiveEnabled"]["defaultValue"]
        )
        self.assertFalse(
            template["parameters"]["speechVoiceLiveEnabled"]["defaultValue"]
        )
        self.assertEqual(
            {"value": "[parameters('speechVoiceLiveEnabled')]"},
            template["resources"]["gateway"]["properties"]["parameters"][
                "speechVoiceLiveEnabled"
            ],
        )
        speech_resource_names = (
            "speechVoiceLiveWssEndpointValue",
            "speechVoiceLiveAudienceValue",
            "sharedSpeechVoiceLiveApi",
            "sharedSpeechVoiceLiveHandshake",
            "sharedSpeechVoiceLiveApiPolicy",
            "sharedSpeechVoiceLiveSubscription",
            "speechVoiceLiveAccount",
            "sharedApimSpeechVoiceLiveFoundryUser",
        )
        for name in speech_resource_names:
            self.assertEqual(
                "[parameters('speechVoiceLiveEnabled')]",
                gateway_resources[name].get("condition"),
                f"{name} must be controlled by the Speech feature flag",
            )
        speech_url_output = gateway_template["outputs"]["speechVoiceLiveGatewayUrl"][
            "value"
        ]
        speech_key_output = gateway_template["outputs"]["speechVoiceLiveGatewayKey"][
            "value"
        ]
        self.assertEqual(
            "[if(parameters('speechVoiceLiveEnabled'), format('{0}/speech/voice-live', parameters('sharedApimGatewayUrl')), '')]",
            speech_url_output,
        )
        self.assertEqual(
            "[if(parameters('speechVoiceLiveEnabled'), listSecrets('sharedSpeechVoiceLiveSubscription', '2024-05-01').primaryKey, '')]",
            speech_key_output,
        )
        self.assertIn("sharedApimCognitiveUsers", gateway_resources)
        self.assertIn("foundryUserRoleId", json.dumps(speech_rbac))
        self.assertTrue(gateway_resources["speechVoiceLiveAccount"].get("existing"))
        # RBAC is a single scalar assignment (one account), never a `copy`/array
        # loop like sharedApimCognitiveUsers/sharedApimOpenAiUsers above.
        self.assertNotIn(
            "copy", gateway_resources["sharedApimSpeechVoiceLiveFoundryUser"]
        )
        self.assertNotIn("sharedApimSpeechVoiceLiveUser", gateway_resources)

        apimcore_template = template["resources"]["apimcore"]["properties"]["template"]
        apimcore_text = json.dumps(apimcore_template["resources"])
        self.assertIn("Microsoft.ApiManagement/service", apimcore_text)
        self.assertIn("Microsoft.Insights/diagnosticSettings", apimcore_text)

        mcp_template = template["resources"]["mcpgateway"]["properties"]["template"]
        mcp_resources = mcp_template["resources"]
        mcp_text = json.dumps(mcp_resources)
        self.assertIn("Microsoft.ApiManagement/service/products/apis", mcp_text)
        self.assertNotIn('"scope": "/apis"', mcp_text)
        self.assertIn("\"condition\": \"[parameters('enableOfficialMcp')]\"", json.dumps(template["resources"]["mcpgateway"]))
        self.assertIn("parameters('apimName')", mcp_text)
        mcp_values = mcp_resources.values() if isinstance(mcp_resources, dict) else mcp_resources
        mcp_types = {resource["type"] for resource in mcp_values if isinstance(resource, dict) and "type" in resource}
        self.assertNotIn("Microsoft.ApiManagement/service", mcp_types)
        self.assertNotIn("Microsoft.Insights/diagnosticSettings", mcp_types)
        self.assertIn("mcpGatewayBaseUrl", mcp_template["outputs"])
        self.assertNotIn("mcpGatewaySubscriptionKey", mcp_template["outputs"])
        self.assertIn("mcpSubscriptionKey", apimcore_template["outputs"])
        apimcore_resources = apimcore_template["resources"]
        apimcore_values = (
            list(apimcore_resources.values())
            if isinstance(apimcore_resources, dict)
            else apimcore_resources
        )
        core_subscription = next(
            resource
            for resource in apimcore_values
            if resource["type"] == "Microsoft.ApiManagement/service/subscriptions"
        )
        self.assertIn("/products/ai4ia-mcp", json.dumps(apimcore_resources))
        self.assertEqual(
            "/products/ai4ia-mcp",
            core_subscription["properties"]["scope"],
        )
        self.assertTrue(
            any(
                resource["type"] == "Microsoft.ApiManagement/service/products"
                for resource in apimcore_values
            )
        )

        resource_types = {resource["type"].lower() for resource in all_resources if "type" in resource}
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
        normalized_copy = next(
            variable
            for variable in template["variables"]["copy"]
            if variable["name"] == "normalizedModelPolicyFragmentDefinitions"
        )
        self.assertEqual(
            "[length(variables('modelPolicyFragmentDefinitions'))]",
            normalized_copy["count"],
        )
        self.assertEqual(
            "[replace(variables('modelPolicyFragmentDefinitions')[copyIndex('normalizedModelPolicyFragmentDefinitions')].value, '\r\n', '\n')]",
            normalized_copy["input"]["value"],
        )
        for resource_name in (
            "modelPolicyFragments",
            "sharedModelPolicyFragments",
        ):
            compiled_fragment = resources[resource_name]
            self.assertIn(
                "normalizedModelPolicyFragmentDefinitions",
                compiled_fragment["name"],
            )
            self.assertEqual(
                "[variables('normalizedModelPolicyFragmentDefinitions')[copyIndex()].value]",
                compiled_fragment["properties"]["value"],
            )
        self.assertIn(
            "reduce(variables('normalizedModelPolicyFragmentDefinitions')",
            template["variables"]["modelApiPolicyValue"],
        )
        compiled_fragment_contract = json.dumps(
            {
                "policy": template["variables"]["modelApiPolicyValue"],
                "legacy": resources["modelPolicyFragments"],
                "shared": resources["sharedModelPolicyFragments"],
            }
        )
        self.assertNotIn(
            "uniqueString(variables('modelPolicyFragmentDefinitions')",
            compiled_fragment_contract,
        )
        self.assertNotIn(
            "variables('modelPolicyFragmentDefinitions')[copyIndex()].value",
            compiled_fragment_contract,
        )
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

    def test_speech_voice_live_requires_master_voice_live_gate(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": False,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("speechVoiceLiveEnabled=true is inert unless voiceLiveEnabled=true", output)

    def test_speech_voice_live_requires_allowlist_membership(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn(
            "requires voiceProviderAllowlist to include speech_voice_live", output
        )

    def test_allowlist_without_enablement_is_rejected(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": False,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("but speechVoiceLiveEnabled is not true", output)

    def test_allowlist_always_requires_azure_openai(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceProviderAllowlist": "speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("must always include azure_openai", output)

    def test_default_provider_must_be_allowlisted(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceProviderAllowlist": "azure_openai",
                "voiceDefaultProvider": "speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("voiceDefaultProvider must be a member of voiceProviderAllowlist", output)

    def test_speech_voice_live_complete_configuration_passes(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
                "voiceDefaultProvider": "azure_openai",
                "realtimeAllowedOrigins": "https://example.test",
            }
        )
        self.assertEqual(result, 0)
        self.assertIn("look sane", output)

    def test_speech_voice_live_audience_must_not_be_blanked(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "speechVoiceLiveManagedIdentityAudience": "",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("speechVoiceLiveManagedIdentityAudience must not be blanked out", output)


if __name__ == "__main__":
    unittest.main()
