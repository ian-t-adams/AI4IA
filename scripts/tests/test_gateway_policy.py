from __future__ import annotations

import importlib.util
import html
import io
import json
import re
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
    def test_anthropic_models_use_the_messages_backend_only(self) -> None:
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        blocks, _ = gateway_generator.render_catalog(models)
        claude = [
            block
            for block in blocks
            if 'new JProperty("claude-opus-4-8-' in block
        ]
        self.assertEqual(len(claude), 2)
        for block in claude:
            self.assertIn('new JProperty("path", "anthropic")', block)
            self.assertNotIn('new JProperty("path", "openai")', block)

        non_claude = next(
            block for block in blocks if 'new JProperty("gpt-5.4-' in block
        )
        self.assertIn('new JProperty("path", "openai")', non_claude)

    def test_anthropic_auth_and_path_are_server_owned(self) -> None:
        setup = gateway_generator.TEMPLATE_PATH.read_text(encoding="utf-8")
        priority = gateway_generator.PRIORITY_POLICY_PATH.read_text(encoding="utf-8")
        self.assertIn("/ai.azure.com", setup)
        self.assertIn('providerPath, &quot;anthropic&quot;', setup)
        self.assertIn("<rewrite-uri template=\"/v1/messages\"", priority)
        self.assertIn('name="anthropic-version" exists-action="override"', priority)
        self.assertIn("<value>2023-06-01</value>", priority)
        self.assertIn('copy-unmatched-params="false"', priority)

    def test_backend_labels_are_unique_within_every_deployment_block(self) -> None:
        """A duplicate backend label is a production outage, not a cosmetic clash.

        Each label becomes a JSON *property name* in the APIM catalog. Newtonsoft
        throws `Can not add property EASTUS2 ... Property with the same name
        already exists` when the expression runs, so APIM answers 500
        (`ExpressionValueEvaluationFailure`) for **every** request through the
        gateway -- chat and embeddings alike -- and SimpleL7Proxy's circuit
        breaker then reports "No active hosts".

        This happened. Adding DataZoneStandard deployments gave 28 model/region
        pairs two deployments in the same region, the label was the region alone,
        and the collision took the whole model plane down. Nothing caught it:
        `--check` only proves the generated file matches its source, and the
        policy is *syntactically* valid C# so the APIM compiler harness accepts
        it too. Only executing a real request finds it.
        """
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        blocks, _ = gateway_generator.render_catalog(models)
        for block in blocks:
            deployment = re.search(r'new JProperty\("([^"]+)", new JObject', block)
            self.assertIsNotNone(deployment)
            assert deployment is not None
            labels = re.findall(r'new JProperty\("([A-Z0-9]+)", new JObject', block)
            self.assertEqual(
                sorted(labels),
                sorted(set(labels)),
                f"duplicate backend label in the block for {deployment.group(1)}",
            )

    def test_generator_rejects_colliding_labels(self) -> None:
        """The guard must fire, not just happen to be satisfied today."""
        models = {
            "naming": {"subscriptionToken": "tok", "skuShort": {"GlobalStandard": "glbl"}},
            "regions": {"eastus2": {"dataZone": "US"}},
            "catalog": [
                {
                    "name": "m",
                    "category": "chat",
                    # Two GlobalStandard deployments in one region: same label twice.
                    "deployments": [
                        {"region": "eastus2", "sku": "GlobalStandard"},
                        {"region": "eastus2", "sku": "GlobalStandard"},
                    ],
                }
            ],
        }
        with self.assertRaises(ValueError) as caught:
            gateway_generator.render_catalog(models)
        self.assertIn("labels collide", str(caught.exception))

    def test_failover_never_leaves_the_requested_residency(self) -> None:
        """A non-global deployment must not fail over outside its own SKU/zone.

        The residency ladder promises that a `DataZoneStandard` request stays in
        its data zone. Listing a `GlobalStandard` deployment as a failover
        backend would quietly serve that request from anywhere the moment the
        first backend hiccuped -- a residency breach with no signal, which is
        worse than an error.
        """
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        regions = models["regions"]
        blocks, _ = gateway_generator.render_catalog(models)
        sku_short = models["naming"]["skuShort"]
        global_suffix = sku_short["GlobalStandard"]

        for block in blocks:
            requested = re.search(r'new JProperty\("([^"]+)", new JObject', block)
            assert requested is not None
            name = requested.group(1)
            suffix = name.rsplit("-", 1)[-1]
            backends = re.findall(r'new JProperty\("deployment", "([^"]+)"', block)
            if suffix == global_suffix:
                continue
            for backend in backends:
                self.assertEqual(
                    backend.rsplit("-", 1)[-1],
                    suffix,
                    f"{name} may fail over to {backend}, which changes residency",
                )
            # And within the same data zone.
            requested_zone = next(
                regions[r]["dataZone"] for r in regions if f"-{r}-" in name
            )
            for backend in backends:
                backend_zone = next(
                    regions[r]["dataZone"] for r in regions if f"-{r}-" in backend
                )
                self.assertEqual(
                    backend_zone,
                    requested_zone,
                    f"{name} may fail over to {backend}, crossing data zones",
                )

    def test_global_deployments_still_fail_over_across_regions(self) -> None:
        """Non-vacuity control.

        Without this, grouping candidates too tightly would satisfy every
        assertion above by giving each deployment exactly one backend and
        silently removing all redundancy.
        """
        models = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        blocks, _ = gateway_generator.render_catalog(models)
        global_suffix = models["naming"]["skuShort"]["GlobalStandard"]
        multi = 0
        for block in blocks:
            requested = re.search(r'new JProperty\("([^"]+)", new JObject', block)
            assert requested is not None
            if requested.group(1).rsplit("-", 1)[-1] != global_suffix:
                continue
            if len(re.findall(r'new JProperty\("deployment", "', block)) > 1:
                multi += 1
        self.assertGreater(
            multi, 0, "no GlobalStandard deployment has a cross-region failover left"
        )

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
        # One fragment loop: the shared Basic v2 plane. This was 2 while the
        # retired Consumption service ran alongside it as a rollback plane.
        self.assertEqual(
            1,
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

    def test_catalog_fragment_count_matches_bicep_and_compiler_script(self) -> None:
        """Pin the shard count across the three files that must agree.

        CATALOG_FRAGMENT_COUNT lives in Python, but Bicep's loadTextContent needs
        literal paths and the compiler smoke-test script hardcodes its own list,
        so neither can import the constant. Bicep deploys only the fragments it
        names: if the generator emitted more shards than gateway.bicep lists, the
        models in the extra shards would vanish from gateway routing silently —
        no build error, no deploy error, just requests failing to resolve a
        backend. This test is the only thing standing between that and prod.
        """
        count = gateway_generator.CATALOG_FRAGMENT_COUNT

        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        compiler = (ROOT / "scripts/test-apim-policy-compiler.ps1").read_text(
            encoding="utf-8"
        )

        for index in range(count):
            path_ref = f"simplel7proxy-endpoints-catalog-{index}.xml"
            fragment_id = f"endpoint_selection_catalog_{index}_32"
            self.assertIn(
                f"loadTextContent('../policies/{path_ref}')",
                gateway,
                f"gateway.bicep does not load catalog shard {index}",
            )
            self.assertIn(
                fragment_id,
                gateway,
                f"gateway.bicep does not declare fragment {fragment_id}",
            )
            self.assertIn(
                f"infra/policies/{path_ref}",
                compiler,
                f"test-apim-policy-compiler.ps1 does not cover catalog shard {index}",
            )

        # And nothing beyond the configured count, so lowering the constant can
        # never leave an orphaned reference to a file the generator stopped writing.
        self.assertNotIn(f"simplel7proxy-endpoints-catalog-{count}.xml", gateway)
        self.assertNotIn(f"simplel7proxy-endpoints-catalog-{count}.xml", compiler)
        self.assertEqual(
            count,
            gateway.count("simplel7proxy-endpoints-catalog-"),
            "gateway.bicep catalog shard references do not match "
            "CATALOG_FRAGMENT_COUNT",
        )

    def test_catalog_has_headroom_before_the_next_shard_is_required(self) -> None:
        """Fail while there is still room to add models, not after.

        chunk_catalog packs greedily, so the last shard is the only one with
        slack. Once every shard is full the next catalog addition raises in
        render_catalog_fragments, which is correct but arrives at the worst
        moment — mid-change, with the three-file edit above still to do. Warn
        early instead by requiring at least one spare shard.
        """
        models = json.loads(gateway_generator.MODELS_PATH.read_text(encoding="utf-8"))
        blocks, _ = gateway_generator.render_catalog(models)
        used = len(gateway_generator.chunk_catalog(blocks))
        self.assertLess(
            used,
            gateway_generator.CATALOG_FRAGMENT_COUNT,
            f"the catalog now fills all {used} configured shards. Raise "
            "CATALOG_FRAGMENT_COUNT in scripts/gen-gateway-policy.py and add the "
            "matching entries to infra/modules/gateway.bicep and "
            "scripts/test-apim-policy-compiler.ps1 before adding more models.",
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

    def test_a_malformed_400_is_a_permanent_error(self) -> None:
        """A 400 must not be retried, throttle a backend, or become a 429.

        The classifier used to call *every* 400 a temporary error. The intent was
        narrow: an Azure OpenAI ``context_length_exceeded`` 400 really is worth
        retrying, because it depends on which backend the request landed on (a PTU
        deployment with a smaller window), and the retry loop deliberately skips
        PTU backends once ``contextWindowExceeded`` is set.

        Widening that to all 400s meant a merely malformed request (an unsupported
        ``reasoning_effort`` value, say) was retried twice, parked a perfectly
        healthy backend for 10 seconds for every other caller in the region, and
        came back to the client as ``429 Requeue Message`` with an EMPTY body, so
        the provider's own explanation of what was wrong was destroyed. The proxy
        then honoured the 429 and retried across backends, multiplying the cost of
        a request that could never succeed.
        """
        for path in (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml",
            ROOT / "infra/policies/simplel7proxy_backend_32.xml",
        ):
            text = path.read_text(encoding="utf-8")
            root = ElementTree.fromstring(text)
            setters = {
                el.get("name"): (el.get("value") or "")
                for el in root.iter("set-variable")
            }

            temp = setters.get("isTempError")
            self.assertIsNotNone(temp, f"{path.name} no longer classifies isTempError")
            assert temp is not None
            # The bug was the bare `statusCode == 400` disjunct.
            self.assertNotRegex(
                temp.replace(" ", ""),
                r"\|\|statusCode==400\|\|",
                f"{path.name}: every 400 is classified as a temporary error again",
            )
            self.assertIn(
                "isRetryableBadRequest",
                temp,
                f"{path.name}: isTempError must gate 400 on the context-length check",
            )
            # The legitimate case must still be retryable.
            self.assertIn("statusCode == 400", temp)

            gate = setters.get("isRetryableBadRequest")
            self.assertIsNotNone(
                gate, f"{path.name} lost the retryable-400 classification"
            )
            assert gate is not None
            self.assertIn("context_length_exceeded", gate)

            perm = setters.get("isPermError")
            self.assertIsNotNone(perm)
            assert perm is not None
            # >= 400, not > 400: a malformed 400 that is neither temp nor perm
            # leaves RetryRemaining true and the retry loop comes straight back.
            self.assertIn(
                "statusCode >= 400",
                perm,
                f"{path.name}: a malformed 400 is neither temporary nor permanent, "
                "so it would still be retried",
            )

    def test_the_context_length_rule_is_defined_exactly_once(self) -> None:
        """The retry decision and the PTU skip must read the same answer.

        Two copies of the "is this 400 a context-length error" body parse could
        disagree about the same response, retrying on a backend the skip logic
        had already ruled out.
        """
        path = ROOT / "infra/policies/simplel7proxy-priority-retry.xml"
        root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
        # Parse rather than grep the raw text: ElementTree drops comments, so
        # prose explaining the rule is not miscounted as a second definition.
        definitions = [
            el.get("name")
            for el in root.iter("set-variable")
            if "context_length_exceeded" in (el.get("value") or "")
        ]
        self.assertEqual(
            definitions,
            ["isRetryableBadRequest"],
            "the context-length rule must be evaluated in exactly one place",
        )

    def test_a_malformed_400_does_not_throttle_the_backend(self) -> None:
        """Throttling parks a backend for all callers; only backend health may do it."""
        for path in (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml",
            ROOT / "infra/policies/simplel7proxy_backend_32.xml",
        ):
            text = path.read_text(encoding="utf-8")
            flat = " ".join(text.split())
            self.assertNotIn(
                "new[] { 429, 408, 400 }",
                flat,
                f"{path.name}: an unqualified 400 still parks a healthy backend",
            )

    def test_a_permanent_error_returns_the_upstream_body(self) -> None:
        """<return-response> replaces the response, so an absent <set-body> returns nothing.

        Verified live: before this, a 400 from Foundry reached the caller as
        ``400 Permanent Error`` with ``Content-Length: 0`` -- correct status, zero
        diagnostic. The provider's body is the only place the actual reason lives
        (which parameter, which value, which content-filter category), so dropping
        it makes every terminal 4xx unactionable from the app.
        """
        for path in (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml",
            ROOT / "infra/policies/simplel7proxy_backend_32.xml",
        ):
            text = path.read_text(encoding="utf-8")
            root = ElementTree.fromstring(text)
            perm_responses = [
                node
                for node in root.iter("return-response")
                if (node.find("set-status") is not None)
                and node.find("set-status").get("reason") == "Permanent Error"
            ]
            self.assertTrue(
                perm_responses,
                f"{path.name}: no permanent-error return-response found",
            )
            for node in perm_responses:
                body = node.find("set-body")
                self.assertIsNotNone(
                    body,
                    f"{path.name}: permanent-error response has no <set-body>, so it "
                    "returns Content-Length: 0 and destroys the provider's diagnostic",
                )
                self.assertIn(
                    "preserveContent: true",
                    (body.text or ""),
                    f"{path.name}: the upstream body must be read with preserveContent",
                )

    def test_tokenprocessor_is_only_set_for_text_response_bodies(self) -> None:
        """TOKENPROCESSOR must never be applied to a binary response body.

        The header tells SimpleL7Proxy to stream the response through
        JsonStreamProcessor, which reads it with a StreamReader and re-emits it with
        WriteLineAsync. That is lossy for non-text: bytes that are not valid UTF-8
        become U+FFFD and raw 0x0D/0x0A bytes are rewritten as the platform newline.
        Setting it unconditionally corrupted every /audio/speech response -- the mp3
        reached the browser as UTF-8 mojibake while the audio/mpeg Content-Type
        survived, so no content-type check anywhere in the chain could detect it and
        playback failed with a bare MEDIA_ERR_SRC_NOT_SUPPORTED.
        """
        for path in (
            ROOT / "infra/policies/simplel7proxy-priority-retry.xml",
            ROOT / "infra/policies/simplel7proxy_outbound_32.xml",
        ):
            root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
            setters = [
                el
                for el in root.iter("set-header")
                if el.get("name") == "TOKENPROCESSOR"
                and el.get("exists-action") != "delete"
            ]
            self.assertTrue(setters, f"{path.name} no longer sets TOKENPROCESSOR")

            # Every setter has to sit under a <when> that inspects the content type.
            parents = {child: parent for parent in root.iter() for child in parent}
            for setter in setters:
                guards = []
                node = setter
                while node in parents:
                    node = parents[node]
                    if node.tag == "when":
                        guards.append(node.get("condition") or "")
                self.assertTrue(
                    any("Content-Type" in g for g in guards),
                    f"{path.name}: TOKENPROCESSOR is set without a Content-Type guard, "
                    "so binary responses will be corrupted by the proxy's text "
                    "stream processor",
                )
                guard = next(g for g in guards if "Content-Type" in g)
                # A guard that does not admit JSON and SSE would silently drop token
                # usage telemetry for every chat call.
                self.assertIn("application/json", guard)
                self.assertIn("text/", guard)

    def test_policy_xml_is_ascii_only(self) -> None:
        """Non-ASCII in a policy file breaks the ARM compile step on Windows.

        ``az bicep build`` emits the embedded policy on stdout using the console
        code page, so a single smart quote or em dash comes back as an undecodable
        byte and the compile reads as an empty template rather than a syntax error.
        Every policy file is ASCII today; keep it that way.
        """
        for path in sorted((ROOT / "infra/policies").glob("*.xml")):
            text = path.read_text(encoding="utf-8")
            offenders = sorted({c for c in text if ord(c) > 127})
            self.assertEqual(
                offenders,
                [],
                f"{path.name} contains non-ASCII characters: "
                + ", ".join(f"U+{ord(c):04X}" for c in offenders),
            )

    def test_documented_model_header_matches_the_policy(self) -> None:
        """deployment.md section 7.11 hardcodes the gateway's model header name.

        The gateway resolves the backend from a *request header*, not from the URL
        path or the body's ``model`` field, and rejects any mismatch with
        ``400 model_path_mismatch``. That is unguessable from the error text, so the
        runbook documents the header by name -- which makes the runbook wrong, in a
        way nothing else would catch, the moment ``modelHeaderName`` is renamed.
        Two separate debugging sessions were lost to this exact call contract.
        """
        header_decl = re.search(
            r'<set-variable name="modelHeaderName" value="([^"]+)"',
            (ROOT / "infra/policies/simplel7proxy-endpoints.xml").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIsNotNone(
            header_decl, "modelHeaderName is no longer a literal set-variable"
        )
        header = header_decl.group(1)

        runbook = (ROOT / "docs/runbooks/deployment.md").read_text(encoding="utf-8")
        self.assertIn(
            header,
            runbook,
            f"deployment.md must document the model header {header!r}",
        )
        # The template the generator renders from has to agree, or a regenerate
        # silently swaps the header out from under both the docs and the app.
        self.assertIn(
            f'<set-variable name="modelHeaderName" value="{header}"',
            (ROOT / "infra/policies/simplel7proxy-endpoints.template.xml").read_text(
                encoding="utf-8"
            ),
        )

    def test_every_azd_parameter_token_is_reachable_from_ci(self) -> None:
        """Every ``${VAR}`` in main.parameters.json must be plumbed through deploy.yml.

        azd only substitutes environment variables that are actually present, so a
        parameter token with no export silently deploys its placeholder default no
        matter what the repo variable says. That is not hypothetical: the live APIM
        was provisioned with ``ai4ia@example.com`` and every resource tagged
        ``owner=ai4ia-operator`` while both repo variables were set correctly,
        because neither was exported. The AI4IA_MEMORY_STORE comment in deploy.yml
        records the same bug being found once before for a single variable.
        """
        params = (ROOT / "infra/main.parameters.json").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        tokens = set(
            re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:=[^}]*)?\}", params)
        )
        # azd owns these itself (`azd env new` / the pipeline login), so they are
        # resolved without an explicit export.
        azd_native = {
            "AZURE_ENV_NAME",
            "AZURE_LOCATION",
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_PRINCIPAL_ID",
        }
        exported = set(re.findall(r"^\s{6}([A-Z][A-Z0-9_]*):", workflow, re.M))
        self.assertGreater(
            len(tokens - azd_native), 25, "parameter token scan looks vacuous"
        )
        missing = sorted(tokens - azd_native - exported)
        self.assertEqual(
            missing,
            [],
            "main.parameters.json reads these but deploy.yml never exports them, so "
            f"they always take their placeholder default: {missing}",
        )

    def test_no_azd_parameter_export_shadows_its_parameter_file_default(self) -> None:
        """A ``|| 'fallback'`` in deploy.yml makes the parameter file's default dead.

        ``${{ vars.X || 'y' }}`` always expands to something non-empty, so azd never
        sees an empty value and never reaches the ``${X=default}`` default in
        main.parameters.json. The workflow becomes a second, invisible source of
        truth for the same setting.

        This is not theoretical and it fails silently by construction. The durable
        workflow flag was flipped to ``true`` in main.parameters.json, CI went green,
        the deploy went green -- and no scheduler was ever provisioned, because
        ``|| 'false'`` on the export pinned it off. Two of the fourteen shadowed
        tokens had already drifted away from the default they shadowed. The comment
        directly above the durable export even said not to add such a fallback,
        while the very next line carried one.

        The existing reachability test cannot catch this: the token IS exported, so
        it looks correctly plumbed.
        """
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        shadowed = re.findall(
            r"^\s+([A-Z][A-Z0-9_]*):\s*\$\{\{\s*vars\.[A-Z0-9_]+\s*\|\|", workflow, re.M
        )
        exports = re.findall(r"^\s+([A-Z][A-Z0-9_]*):\s*\$\{\{\s*vars\.", workflow, re.M)
        self.assertGreater(
            len(exports), 25, "export scan looks vacuous; the pattern stopped matching"
        )
        self.assertEqual(
            sorted(shadowed),
            [],
            "these deploy.yml exports carry a `|| fallback`, which shadows the "
            "${VAR=default} in main.parameters.json and makes that default dead: "
            f"{sorted(shadowed)}. Drop the fallback and let the parameter file own "
            "the default -- azd already resolves an empty value to it.",
        )

    def test_pre_routing_failures_are_not_reported_as_throttling(self) -> None:
        """A request that dies before backend selection must not be laundered into a 429.

        Observed live: an APIM subscription-key rejection never reaches the
        endpoint-selection fragments, so ``listBackends`` stays empty, the
        throttling loop counts zero un-throttled backends, and on-error answers
        ``429 No Backends Available`` with ``S7PREQUEUE``/``retry-after-ms`` and an
        empty ``X-Policy-LastError``. That inverts the diagnosis -- it points the
        operator at backend capacity for what is actually a rejected credential --
        and tells the caller to retry a request that can never succeed.
        """
        for relative in (
            "infra/policies/simplel7proxy-priority-retry.xml",
            "infra/policies/simplel7proxy_on_error_32.xml",
        ):
            with self.subTest(policy=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                # The master carries an outbound copy of the throttling verdict too,
                # but that one defaults unThrottledBackends to -1 and so cannot
                # misfire on an unrouted request. Scope the assertions to on-error.
                start = text.find("<on-error>")
                policy = text[start if start >= 0 else 0 :]
                self.assertIn(
                    'name="preRoutingFailure"',
                    policy,
                    "on-error must classify failures that never reached routing",
                )
                # The empty-list guard has to run BEFORE the throttling verdict,
                # otherwise the 429 still wins.
                guard = policy.index("preRoutingFailure&quot;, false)) {")
                throttle = policy.index("if (unthrottledBackends == 0) {")
                self.assertLess(
                    guard,
                    throttle,
                    "the pre-routing guard must short-circuit the throttling branch",
                )
                # LastError must survive even when the inbound fragments never ran
                # and so never appended to lastPolicyError.
                last_error = policy.index('name="X-Policy-LastError"')
                self.assertIn(
                    "context.LastError?.Reason",
                    policy[last_error:],
                    "X-Policy-LastError must fall back to the real APIM error",
                )
                # Requeue/retry hints are only meaningful for a throttled backend.
                for header in ('name="S7PREQUEUE"', 'name="retry-after-ms"'):
                    self.assertLess(
                        policy.index(
                            'condition="@(!context.Variables.GetValueOrDefault'
                            "&lt;bool>(&quot;preRoutingFailure&quot;, false))\""
                        ),
                        policy.index(header),
                        f"{header} must be suppressed for pre-routing failures",
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

    def test_shared_basic_v2_apim_is_the_only_gateway_plane(self) -> None:
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        apimcore = (ROOT / "infra/modules/apimcore.bicep").read_text(encoding="utf-8")
        mcp = (ROOT / "infra/modules/mcpgateway.bicep").read_text(encoding="utf-8")
        main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        api = (ROOT / "infra/modules/api.bicep").read_text(encoding="utf-8")

        # The retired Consumption service and every child it owned are gone. It was
        # kept as an inactive rollback plane through the Basic v2 migration and
        # deleted once the shared service had carried production traffic. Leaving
        # the Bicep behind would recreate a second billable gateway on next deploy.
        self.assertNotIn("legacyConsumptionApim", gateway)
        self.assertNotIn("name: 'Consumption'", gateway)
        self.assertNotIn("realtime-routing-legacy", gateway)
        self.assertFalse((ROOT / "infra/policies/realtime-routing-legacy.xml").exists())
        for retired in (
            "resource foundryEndpointValues", "resource modelPolicyFragments",
            "resource modelsApi ", "resource modelOperations",
            "resource modelsApiPolicy", "resource proxyModelSubscription",
            "resource realtimeApi ", "resource realtimeOperation",
            "resource realtimeApiPolicy", "resource apiRealtimeSubscription",
            "resource apimOpenAiUsers", "resource apimCognitiveUsers",
        ):
            self.assertNotIn(retired, gateway)
        # gateway creates no APIM service now, so it needs neither the publisher
        # fields nor the uniqueSuffix that only the Consumption service name consumed.
        # `workload` is deliberately NOT retired: gateway no longer uses it to build a
        # service name, but it does use it to derive the APIM *child* entity names, which
        # share one flat namespace on the shared plane.
        for retired_param in (
            "param apimPublisherEmail", "param apimPublisherName",
            "param uniqueSuffix string",
        ):
            self.assertNotIn(retired_param, gateway)
        self.assertIn("param workload string", gateway)
        self.assertNotIn("take('apim-mcp-${workload}", gateway)
        # apimcore still owns the one real service, its publisher identity, and the
        # uniqueSuffix that keeps globally-unique APIM names deployable elsewhere.
        self.assertIn("param apimPublisherEmail", apimcore)
        self.assertIn("param apimPublisherName", apimcore)
        self.assertIn(
            "name: take('apim-mcp-${workload}-${environmentName}-${uniqueSuffix}', 50)",
            apimcore,
        )

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

        self.assertTrue(
            "name: take('apim-mcp-${workload}-${environmentName}-${uniqueSuffix}', 50)" in apimcore,
            "apimcore must name the shared BasicV2 service with the uniqueness suffix",
        )
        self.assertIn("name: 'BasicV2'", apimcore)
        self.assertIn("resource apimDiagnostics", apimcore)

        self.assertIn("param apimName string", mcp)
        self.assertIn("resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing", mcp)
        self.assertNotIn("param enableOfficialMcp", mcp)
        self.assertNotIn("resource apimDiagnostics", mcp)
        self.assertNotIn("name: 'BasicV2'", mcp)
        self.assertNotIn("take('apim-mcp-${workload}", mcp)
        self.assertIn("resource mcpProduct", mcp)
        self.assertIn("resource mcpProductApis", mcp)
        self.assertIn("scope: '/products/${mcpProductName}'", apimcore)
        self.assertNotIn("scope: '/products/", mcp)
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
        self.assertIn("scope: '/products/${proxyIngressProductName}'", gateway)
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
        self.assertIn("name: speechVoiceLiveSubscriptionName", gateway)
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
        self.assertIn("<choose>", policy)
        self.assertIn('<set-status code="400"', policy)
        self.assertIn("String.IsNullOrWhiteSpace", policy)
        self.assertIn("StringComparison.Ordinal", policy)
        for model_id in (
            "gpt-realtime",
            "gpt-realtime-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-5-mini",
            "gpt-5.1",
        ):
            self.assertIn(f"&quot;{model_id}&quot;.Equals(model", policy)
        self.assertNotIn("<set-body>", policy)
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

        # Every pre-existing plane (the shared active /openai + /openai/realtime
        # APIs and their subscriptions) is untouched: still present and unrenamed.
        for untouched in (
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
        self.assertIn("name: speechVoiceLiveSubscriptionName", gateway)

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
        self.assertIn('? "gpt-realtime" :', policy)
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

    def test_compiled_arm_creates_exactly_one_apim_service(self) -> None:
        template = self._build_bicep_template(ROOT / "infra/main.bicep")
        all_resources = self._collect_resources(template)
        service_resources = [
            resource for resource in all_resources
            if resource.get("type", "").lower() == "microsoft.apimanagement/service"
            and not resource.get("existing")
        ]
        # Exactly one billable gateway. This was 2 while the Consumption service
        # was retained as a rollback plane during the Basic v2 migration; that
        # service has been deleted, so a second one here would silently recreate it.
        self.assertEqual(1, len(service_resources))
        serialized = json.dumps(template)
        self.assertNotIn("apim-v2-", serialized)
        self.assertNotIn("apim-rt-", serialized)
        shared = service_resources[0]
        # Scoped to APIM SKUs: "Consumption" is also a Container Apps workload
        # profile name, so a whole-template search would false-positive.
        self.assertNotIn(
            "Consumption",
            json.dumps([resource.get("sku") for resource in service_resources]),
        )
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
        self.assertIn(
            "variables('speechVoiceLiveSubscriptionName')",
            json.dumps(gateway_resources["sharedSpeechVoiceLiveSubscription"]["name"]),
        )
        # Resolving through the variable proves the workload-derived name still yields
        # the original 'ai4ia-api-speech-voice-live' for the default workload, so the
        # parameterization did not silently rename (and re-key) a live subscription.
        self.assertEqual(
            "[format('{0}-api-speech-voice-live', parameters('workload'))]",
            gateway_template["variables"]["speechVoiceLiveSubscriptionName"],
        )
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
        self.assertIn("/products/", json.dumps(apimcore_resources))
        self.assertEqual(
            "[format('/products/{0}', variables('mcpProductName'))]",
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
            "sharedModelPolicyFragments",
            resources["sharedModelsApiPolicy"]["dependsOn"],
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
        fragment_resource = resources["sharedModelPolicyFragments"]
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
        compiled_fragment = resources["sharedModelPolicyFragments"]
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
            resources["sharedModelsApiPolicy"]["properties"]["value"],
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

    def test_priority_reservation_uses_the_env_name_the_parser_reads(self) -> None:
        """gateway.bicep must emit `PriorityWorkers` (plural), not `PriorityWorker`.

        Only the plural key reaches `PriorityWorkerDict`, which is what
        `WorkerFactory` reserves from. The singular name is a real property on
        `ProxyConfig` but nothing converts it into that dictionary, so it parses,
        validates, and is discarded -- leaving band 1 (admins) with zero reserved
        workers while every surface reports the feature as enabled. The parser
        side of this coupling is pinned by
        `proxy/AI4IA.Proxy.Tests/PriorityWorkerConfigTests.cs`.
        """
        gateway = (ROOT / "infra/modules/gateway.bicep").read_text(encoding="utf-8")
        self.assertIn(
            "{ name: 'PriorityWorkers', value: proxyPrioritiesEnabled "
            "? proxyPriorityWorkers : '' }",
            gateway,
        )
        # The singular name must not come back, including as a second "belt and
        # braces" entry -- two variables that look interchangeable but are not is
        # exactly what made this inert the first time.
        self.assertNotIn("name: 'PriorityWorker'", gateway)

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
            "module eventhubs 'modules/eventhubs.bicep' = if (proxyEventHubTelemetryEnabled)",
            main,
        )
        self.assertNotIn("telemetrySenderPrincipalIds", main)

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
        parameters = {
            "claudeOrganizationName": "Example Legal Entity",
            "claudeCountryCode": "US",
            "claudeIndustry": "technology",
            **parameters,
        }
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
        # No realtimeAllowedOrigins here on purpose: main.bicep now derives the
        # allowlist from the web app this deployment creates, and the validator
        # rejects a literal hostname pinned in parameters as tenant-coupled.
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
                "voiceDefaultProvider": "azure_openai",
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
