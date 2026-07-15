from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
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
        (
            priority_expected,
            expected_priority_fragments,
            priority_populated_count,
        ) = gateway_generator.generate_priority_policy()
        priority_policy = gateway_generator.PRIORITY_POLICY_PATH.read_text(
            encoding="utf-8"
        )
        self.assertEqual(priority_policy, priority_expected)
        for path, priority_fragment_expected in zip(
            gateway_generator.PRIORITY_OUTPUT_PATHS,
            expected_priority_fragments,
            strict=True,
        ):
            priority_fragment = path.read_text(encoding="utf-8")
            self.assertEqual(priority_fragment, priority_fragment_expected)
            gateway_generator.validate_policy_fragment(
                priority_fragment,
                str(path.relative_to(ROOT)),
            )
        ElementTree.fromstring(priority_policy)
        gateway_generator.validate_policy_expressions(
            priority_policy,
            "infra/policies/simplel7proxy-priority-retry.xml",
        )
        gateway_generator.validate_policy_payload(
            priority_policy,
            "infra/policies/simplel7proxy-priority-retry.xml",
            max_bytes=gateway_generator.APIM_POLICY_DOCUMENT_MAX_BYTES,
            payload_kind="policy document",
        )
        gateway_generator.validate_fragment_include_chain(
            priority_policy,
            "infra/policies/simplel7proxy-priority-retry.xml",
        )
        gateway_generator.validate_priority_include_chain(
            priority_policy,
            "infra/policies/simplel7proxy-priority-retry.xml",
            priority_populated_count,
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
        gateway_generator.validate_policy_payload(
            realtime_output,
            "infra/policies/realtime-routing.xml",
            max_bytes=gateway_generator.APIM_POLICY_DOCUMENT_MAX_BYTES,
            payload_kind="policy document",
        )

    def test_split_priority_policy_preserves_statement_tree(self) -> None:
        generated_policy, generated_fragments, populated_count = (
            gateway_generator.generate_priority_policy()
        )
        template = gateway_generator.normalize_policy_expression_comments(
            gateway_generator.PRIORITY_TEMPLATE_PATH.read_text(encoding="utf-8")
        )
        template_root = ElementTree.fromstring(template)
        generated_root = ElementTree.fromstring(generated_policy)
        fragment_roots = {
            gateway_generator.PRIORITY_FRAGMENT_IDS[index]: (
                ElementTree.fromstring(generated_fragments[index])
            )
            for index in range(populated_count)
        }

        def normalized_text(value: str | None) -> str:
            return value.strip() if value and value.strip() else ""

        def canonical_children(
            element: ElementTree.Element,
            *,
            expand_priority: bool,
        ) -> list[tuple[object, ...]]:
            result = []
            for child in element:
                if (
                    expand_priority
                    and child.tag == "include-fragment"
                    and child.attrib.get("fragment-id") in fragment_roots
                ):
                    result.extend(
                        canonical_children(
                            fragment_roots[child.attrib["fragment-id"]],
                            expand_priority=expand_priority,
                        )
                    )
                    continue
                result.append(
                    (
                        child.tag,
                        tuple(sorted(child.attrib.items())),
                        normalized_text(child.text),
                        tuple(
                            canonical_children(
                                child,
                                expand_priority=expand_priority,
                            )
                        ),
                    )
                )
            return result

        expected = (
            template_root.tag,
            tuple(sorted(template_root.attrib.items())),
            normalized_text(template_root.text),
            tuple(canonical_children(template_root, expand_priority=False)),
        )
        actual = (
            generated_root.tag,
            tuple(sorted(generated_root.attrib.items())),
            normalized_text(generated_root.text),
            tuple(canonical_children(generated_root, expand_priority=True)),
        )
        self.assertEqual(expected, actual)
        self.assertLess(populated_count, gateway_generator.PRIORITY_FRAGMENT_COUNT)

    def test_fragment_generation_manifest_pins_immutable_outputs(self) -> None:
        setup_fragment, catalog_fragments = (
            gateway_generator.generate_endpoint_policies()
        )
        priority_policy, priority_fragments, _ = (
            gateway_generator.generate_priority_policy()
        )
        outputs = (
            (gateway_generator.OUTPUT_PATH, setup_fragment),
            *zip(
                gateway_generator.CATALOG_OUTPUT_PATHS,
                catalog_fragments,
                strict=True,
            ),
            *zip(
                gateway_generator.PRIORITY_OUTPUT_PATHS,
                priority_fragments,
                strict=True,
            ),
            (gateway_generator.PRIORITY_POLICY_PATH, priority_policy),
        )
        digest = gateway_generator.policy_generation_digest(outputs)
        manifest = json.loads(
            gateway_generator.POLICY_GENERATION_MANIFEST_PATH.read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            manifest["generation"],
            gateway_generator.POLICY_FRAGMENT_GENERATION,
        )
        self.assertEqual(manifest["sha256"], digest)
        self.assertFalse(
            gateway_generator.validate_generation_manifest(
                digest,
                check=True,
            )
        )
        with self.assertRaisesRegex(
            ValueError,
            "immutable policy fragment generation",
        ):
            gateway_generator.validate_generation_manifest(
                "0" * 64,
                check=True,
            )

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

    def test_every_generated_fragment_stays_below_deployment_ceiling(self) -> None:
        setup_fragment, catalog_fragments = (
            gateway_generator.generate_endpoint_policies()
        )
        _, priority_fragments, _ = gateway_generator.generate_priority_policy()
        for fragment in (
            *catalog_fragments,
            setup_fragment,
            *priority_fragments,
        ):
            self.assertLessEqual(
                gateway_generator.policy_payload_bytes(fragment),
                gateway_generator.APIM_POLICY_FRAGMENT_MAX_BYTES,
            )
            self.assertLessEqual(
                gateway_generator.policy_payload_bytes_with_crlf(fragment),
                gateway_generator.APIM_POLICY_FRAGMENT_MAX_BYTES,
            )

    def test_catalog_split_is_lossless_deterministic_and_uses_placeholders(
        self,
    ) -> None:
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        blocks, _ = gateway_generator.render_catalog(models)
        setup_fragment, catalog_fragments = (
            gateway_generator.generate_endpoint_policies()
        )
        populated_chunks = gateway_generator.chunk_catalog(blocks)
        self.assertLess(
            len(populated_chunks),
            gateway_generator.CATALOG_FRAGMENT_COUNT,
        )
        expected_deployments = []
        naming = models["naming"]
        for model in models["catalog"]:
            for deployment in model["deployments"]:
                expected_deployments.append(
                    gateway_generator.deployment_name(
                        model=model["name"],
                        subscription_token=naming["subscriptionToken"],
                        region=deployment["region"],
                        sku=deployment["sku"],
                        sku_short=naming["skuShort"],
                    )
                )

        deployment_pattern = re.compile(
            r"^ {12}new JProperty\(&quot;([^&]+)&quot;, new JObject\(",
            re.MULTILINE,
        )
        actual_deployments = []
        chunk_names = []
        for index, fragment in enumerate(catalog_fragments):
            root = ElementTree.fromstring(fragment)
            variable = root.find("set-variable")
            self.assertIsNotNone(variable)
            assert variable is not None
            self.assertEqual(variable.attrib["name"], f"backendCatalogChunk{index}")
            chunk_names.append(variable.attrib["name"])
            actual_deployments.extend(deployment_pattern.findall(fragment))
            if index >= len(populated_chunks):
                self.assertIn("return new JObject();", variable.attrib["value"])

        self.assertEqual(
            Counter(expected_deployments),
            Counter(actual_deployments),
        )
        self.assertEqual(len(actual_deployments), len(set(actual_deployments)))
        setup_root = ElementTree.fromstring(setup_fragment)
        merge = next(
            element.attrib["value"]
            for element in setup_root.findall("set-variable")
            if element.attrib.get("name") == "backendCatalog"
        )
        merge_positions = [
            merge.index(f'context.Variables["{name}"]') for name in chunk_names
        ]
        self.assertEqual(merge_positions, sorted(merge_positions))
        for name in chunk_names:
            self.assertEqual(merge.count(f'context.Variables["{name}"]'), 1)

    def test_catalog_chunker_rejects_single_payload_above_safe_ceiling(self) -> None:
        oversized_block = (
            '            new JProperty("oversized", new JObject(\n'
            + ("x" * (gateway_generator.APIM_POLICY_FRAGMENT_MAX_BYTES + 1))
            + "\n            ))"
        )
        with self.assertRaisesRegex(ValueError, "fragment deployment limit"):
            gateway_generator.chunk_catalog([oversized_block])

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

    def test_fragment_validator_rejects_base_and_nested_fragments(self) -> None:
        for forbidden in (
            "<base />",
            '<include-fragment fragment-id="nested" />',
        ):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(
                    ValueError,
                    "policy fragments cannot contain",
                ):
                    gateway_generator.validate_policy_fragment(
                        f"<fragment>{forbidden}</fragment>",
                        "forbidden-fragment.xml",
                    )

    def test_fragment_validator_rejects_payload_above_16_kib(self) -> None:
        oversized = "<fragment>" + ("x" * (16 * 1024)) + "</fragment>"
        with self.assertRaisesRegex(
            ValueError,
            rf"safe ceiling is {gateway_generator.APIM_POLICY_FRAGMENT_MAX_BYTES}",
        ):
            gateway_generator.validate_policy_fragment(
                oversized,
                "oversized-fragment.xml",
            )

    def test_policy_validator_rejects_payload_above_16_kib(self) -> None:
        oversized = "<policies>" + ("x" * (16 * 1024)) + "</policies>"
        with self.assertRaisesRegex(
            ValueError,
            rf"safe ceiling is {gateway_generator.APIM_POLICY_DOCUMENT_MAX_BYTES}",
        ):
            gateway_generator.validate_policy_payload(
                oversized,
                "oversized-policy.xml",
                max_bytes=gateway_generator.APIM_POLICY_DOCUMENT_MAX_BYTES,
                payload_kind="policy document",
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
        policy = "\n".join(
            [
                gateway_generator.PRIORITY_POLICY_PATH.read_text(
                    encoding="utf-8"
                ),
                *[
                    path.read_text(encoding="utf-8")
                    for path in gateway_generator.PRIORITY_OUTPUT_PATHS
                ],
            ]
        )
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
        for index, path in enumerate(gateway_generator.PRIORITY_OUTPUT_PATHS):
            self.assertIn(
                f"loadTextContent('../policies/{path.name}')",
                gateway,
            )
            self.assertIn(
                gateway_generator.PRIORITY_FRAGMENT_IDS[index],
                gateway,
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
        self.assertNotIn("endpoint_selection_setup_32", policy)
        self.assertNotIn("endpoint_selection_catalog_0_32", policy)
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
        def resolve_compiled_value(value: str) -> str:
            variable_reference = re.fullmatch(
                r"\[variables\('([^']+)'\)\]",
                value,
            )
            if variable_reference:
                return template["variables"][variable_reference.group(1)]
            return value

        self.assertIn(
            "endpointSelectionFragments",
            resources["modelsApiPolicy"]["dependsOn"],
        )
        self.assertIn(
            "priorityPolicyFragments",
            resources["modelsApiPolicy"]["dependsOn"],
        )
        definitions = template["variables"]["endpointSelectionFragmentDefinitions"]
        priority_definitions = template["variables"][
            "priorityPolicyFragmentDefinitions"
        ]
        self.assertEqual(
            [
                *gateway_generator.CATALOG_FRAGMENT_IDS,
                gateway_generator.SETUP_FRAGMENT_ID,
            ],
            [definition["name"] for definition in definitions],
        )
        self.assertEqual(
            list(gateway_generator.PRIORITY_FRAGMENT_IDS),
            [definition["name"] for definition in priority_definitions],
        )
        for definition in (*definitions, *priority_definitions):
            self.assertLessEqual(
                gateway_generator.policy_payload_bytes(
                    resolve_compiled_value(definition["value"])
                ),
                gateway_generator.APIM_POLICY_FRAGMENT_MAX_BYTES,
            )
        compiled_policies = {}
        for resource_name in ("modelsApiPolicy", "realtimeApiPolicy"):
            policy_value = resolve_compiled_value(
                resources[resource_name]["properties"]["value"]
            )
            compiled_policies[resource_name] = policy_value
            self.assertLessEqual(
                gateway_generator.policy_payload_bytes(policy_value),
                gateway_generator.APIM_POLICY_DOCUMENT_MAX_BYTES,
            )
        compiled_model_policy = compiled_policies["modelsApiPolicy"]
        include_positions = [
            compiled_model_policy.index(f'fragment-id="{fragment_id}"')
            for fragment_id in (
                *gateway_generator.CATALOG_FRAGMENT_IDS,
                gateway_generator.SETUP_FRAGMENT_ID,
            )
        ]
        self.assertEqual(include_positions, sorted(include_positions))
        _, _, priority_populated_count = (
            gateway_generator.generate_priority_policy()
        )
        priority_include_positions = [
            compiled_model_policy.index(f'fragment-id="{fragment_id}"')
            for fragment_id in gateway_generator.PRIORITY_FRAGMENT_IDS[
                :priority_populated_count
            ]
        ]
        self.assertEqual(
            priority_include_positions,
            sorted(priority_include_positions),
        )
        self.assertIn(
            "endpointSelectionFragments",
            resources["modelsApiPolicy"]["dependsOn"],
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
        self.assertIn("LOCAL_POLICY_SIZE=", harness)
        self.assertIn("LIVE_APIM_FULL_CHAIN_COMPILER=PASS", harness)
        self.assertIn("LIVE_APIM_REALTIME_COMPILER=PASS", harness)
        self.assertIn("APIM_POLICY_FRAGMENT_MAX_BYTES", harness)
        self.assertIn("APIM_POLICY_DOCUMENT_MAX_BYTES", harness)
        self.assertNotIn("azd provision", harness)
        self.assertNotIn("az deployment", harness)
        self.assertIn("foreach ($index in 0..9)", harness)
        self.assertIn(
            'ProductionId = "endpoint_selection_catalog_$($index)_33"',
            harness,
        )
        self.assertIn(
            'ProductionId = "priority_policy_$($index)_33"',
            harness,
        )
        self.assertIn(gateway_generator.SETUP_FRAGMENT_ID, harness)

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
