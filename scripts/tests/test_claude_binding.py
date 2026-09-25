"""Source/target separation, exact binding and actual generated-policy controls."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _claude_binding as claude
from _capacity_evidence import EvidenceError, ProcessResult
from _model_targets import source_catalog
from scripts.tests._claude_fixture import FixtureReader, binding, environment, models
from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
AVAILABILITY = load_script("claude_availability", ROOT / "scripts" / "check-model-availability.py")
GENERATOR = load_script("claude_catalog", ROOT / "scripts" / "gen-model-catalog.py")


@contextmanager
def binding_environment(values):
    # Restore only these fixture keys. Clearing all process variables can drop
    # empty Git config values from Windows' native child-process environment.
    before = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ClaudeBindingTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.reader = FixtureReader(self.binding)

    def verify(self):
        claude.verify_identity(self.binding, self.reader, attached=True)
        self.assertEqual(claude.verify_target(self.binding, models(), self.reader), 5)
        claude.verify_routes(self.binding, self.reader, enabled=True)

    def test_exact_active_binding_reads_both_scopes_and_all_generated_fragments(self):
        self.verify()
        self.assertGreater(len(self.reader.calls), 25)
        self.assertTrue(any(call[0] for call in self.reader.calls))
        self.assertTrue(any(not call[0] for call in self.reader.calls))

    def test_default_configuration_constructs_no_reader(self):
        with binding_environment({
            "AI4IA_CLAUDE_ENABLED": "false", "AI4IA_CLAUDE_EXTERNAL_ENABLED": "false",
        }), patch.object(claude, "Reader") as reader:
            self.assertEqual(claude.verify_configured(models(), routed=True), 0)
            reader.assert_not_called()
        with binding_environment(environment(self.binding)), patch.object(
            claude, "Reader", return_value=self.reader,
        ) as reader:
            self.assertEqual(claude.verify_configured(models(), routed=True), 5)
            reader.assert_called_once()

    def test_missing_configuration_is_not_activation_evidence(self):
        good = environment(self.binding)
        self.assertEqual(claude.configured_binding(good), self.binding)
        for change in (
            {"AI4IA_CLAUDE_BINDING_JSON": "{}"}, {"AZURE_TENANT_ID": self.binding["targetTenantId"]},
            {"AI4IA_CLAUDE_EXTERNAL_ENABLED": "false"}, {"AZURE_ENV_NAME": "changed"},
            {"AI4IA_CLAUDE_EXTERNAL_ENABLED": "yes"},
        ):
            with self.subTest(change=change), self.assertRaises(EvidenceError):
                claude.configured_binding({**good, **change})

    def test_authenticated_empty_graph_result_is_not_an_exact_principal_binding(self):
        key = (True, "/servicePrincipals/" + self.binding["targetPrincipalId"])
        original = self.reader.responses[key]
        for empty in ({}, {"value": []}, {"id": self.binding["targetPrincipalId"]}):
            self.reader.responses[key] = empty
            with self.assertRaisesRegex(EvidenceError, "claude_target_principal_mismatch"):
                claude.verify_identity(self.binding, self.reader, attached=True)
            self.reader.responses[key] = original
            claude.verify_identity(self.binding, self.reader, attached=True)

    def test_each_identity_and_resource_defect_refuses_with_identical_positive_control(self):
        b = self.binding
        source_app = (False, "/applications/" + b["applicationObjectId"])
        fic_path = (False, source_app[1] + "/federatedIdentityCredentials")
        cases = [
            ((False, b["sourceIdentityResourceId"]), ["properties", "tenantId"], b["targetTenantId"]),
            ((False, b["sourceIdentityResourceId"]), ["properties", "principalId"], b["sourceIdentityClientId"]),
            ((False, b["sourceApimResourceId"]), ["identity", "type"], "UserAssigned"),
            ((False, b["sourceApimResourceId"]), ["identity", "principalId"], b["sourceIdentityPrincipalId"]),
            ((False, b["sourceApimResourceId"]), ["identity", "userAssignedIdentities"], {}),
            (source_app, ["appId"], b["sourceIdentityClientId"]),
            (source_app, ["signInAudience"], "AzureADMyOrg"),
            (source_app, ["passwordCredentials"], [{"keyId": "synthetic"}]),
            (source_app, ["keyCredentials"], [{"keyId": "synthetic"}]),
            (fic_path, ["value", 0, "subject"], b["sourceIdentityClientId"]),
            (fic_path, ["value", 0, "issuer"], f"https://login.microsoftonline.com/{b['targetTenantId']}/v2.0"),
            (fic_path, ["value", 0, "audiences"], ["https://ai.azure.com"]),
            ((True, "/servicePrincipals/" + b["targetPrincipalId"]), ["appId"], b["sourceIdentityClientId"]),
            ((True, b.account_id), ["properties", "disableLocalAuth"], False),
            ((True, b.account_id), ["tags", "ai4ia-target"], "learning"),
            ((True, b.account_id), ["location"], "swedencentral"),
            ((True, b.account_id + "/deployments"), ["value", 0, "properties", "model", "version"], "1"),
            ((True, b.account_id + "/deployments"), ["value", 0, "sku", "capacity"], 1),
            ((True, b.account_id + "/deployments"), ["value", 0, "sku", "capacity"], 40.0),
            ((True, b.account_id + "/deployments"), ["value", 0, "properties", "provisioningState"], "Creating"),
        ]
        for key, path, replacement in cases:
            with self.subTest(path=path):
                original = deepcopy(self.reader.responses[key])
                row = self.reader.responses[key]
                for part in path[:-1]:
                    row = row[part]
                row[path[-1]] = replacement
                with self.assertRaises(EvidenceError):
                    self.verify()
                self.reader.responses[key] = original
                self.verify()

    def test_exact_role_refuses_key_secret_management_and_additional_data_permissions(self):
        key = (True, self.binding["targetInferenceRoleDefinitionId"])
        good = deepcopy(self.reader.responses[key])
        for category, action in (
            ("actions", "Microsoft.CognitiveServices/accounts/listkeys/action"),
            ("actions", "Microsoft.CognitiveServices/accounts/deployments/write"),
            ("actions", "Microsoft.CognitiveServices/accounts/projects/connections/listsecrets/action"),
            ("dataActions", "Microsoft.CognitiveServices/*"),
        ):
            with self.subTest(action=action):
                self.reader.responses[key]["properties"]["permissions"][0][category].append(action)
                with self.assertRaisesRegex(EvidenceError, "claude_inference_role_permissions"):
                    claude.verify_target(self.binding, models(), self.reader)
                self.reader.responses[key] = deepcopy(good)
                self.assertEqual(claude.verify_target(self.binding, models(), self.reader), 5)

    def test_scope_and_route_changes_are_not_an_enablement_boolean(self):
        b = self.binding
        for key, replacement in (
            ((False, b["sourceApimResourceId"] + "/subscriptions/ai4ia-proxy-models"),
             {"properties": {"scope": "/apis", "state": "active", "allowTracing": False}}),
            ((False, b["sourceApimResourceId"] + "/policyFragments/claude_auth_v1-fixture000000"),
             {"properties": {"value": "<fragment/>"}}),
        ):
            original = self.reader.responses[key]
            self.reader.responses[key] = replacement
            with self.assertRaises(EvidenceError):
                claude.verify_routes(b, self.reader, enabled=True)
            self.reader.responses[key] = original
            claude.verify_routes(b, self.reader, enabled=True)
        subscription = self.reader.responses[(False, b["sourceApimResourceId"] + "/subscriptions/ai4ia-proxy-models")]
        subscription["properties"]["scope"] = "/apis/openai"
        claude.verify_routes(b, self.reader, enabled=True)
        subscription["properties"]["scope"] = "/apis/openai-extra"
        with self.assertRaisesRegex(EvidenceError, "claude_proxy_subscription_scope"):
            claude.verify_routes(b, self.reader, enabled=True)

    def test_binding_change_requires_observed_disabled_policy_not_an_operator_flag(self):
        b = self.binding
        claude.verify_binding_transition(b, self.reader)
        values = self.reader.responses[(False, b["sourceApimResourceId"] + "/namedValues")]["value"]
        values[0]["properties"]["value"] = "https://mf-claude-prior.services.ai.azure.com"
        with binding_environment({"AI4IA_CLAUDE_ENABLED": "false"}):
                with self.assertRaisesRegex(EvidenceError, "disable_old_binding"):
                    claude.verify_binding_transition(b, self.reader)
        disabled = FixtureReader(b, enabled=False)
        disabled.responses[(False, b["sourceApimResourceId"] + "/namedValues")]["value"] = values
        claude.verify_binding_transition(b, disabled)

    def test_policy_readbacks_accept_only_xml_serializer_formatting(self):
        for enabled in (True, False):
            reader = FixtureReader(self.binding, enabled=enabled)
            changed = 0
            for (_, path), response in reader.responses.items():
                if path.endswith("/policies/policy") or "/policyFragments/" in path:
                    original = response["properties"]["value"]
                    root = ET.fromstring(original)
                    ET.indent(root, space=" ")
                    formatted = ET.tostring(root, encoding="unicode").replace("\n", "\r\n").rstrip()
                    self.assertNotEqual(formatted, original)
                    response["properties"]["value"] = formatted
                    changed += 1
            self.assertGreater(changed, 15)
            claude.verify_routes(self.binding, reader, enabled=enabled)
            values = reader.responses[(False, self.binding["sourceApimResourceId"] + "/namedValues")]["value"]
            values[0]["properties"]["value"] = "https://mf-claude-prior.services.ai.azure.com"
            if enabled:
                with self.assertRaisesRegex(EvidenceError, "disable_old_binding"):
                    claude.verify_binding_transition(self.binding, reader)
            else:
                claude.verify_binding_transition(self.binding, reader)

    def test_policy_comparison_preserves_expressions_literals_body_and_child_order(self):
        source = (
            '<fragment><!-- ignored formatting comment -->\n'
            '<choose><when condition=\'@(context.Subscription.Id == "scope with space")\'>\n'
            '<set-header name="Authorization" exists-action="override"><value>'
            '@("Bearer " + (string)context.Variables["target-token"])'
            '</value></set-header><set-body> {"code": "denied"} </set-body>\n'
            '<set-variable name="bound" value=\'@{ return "two  spaces"; }\' />'
            '</when></choose></fragment>\n'
        )
        root = ET.fromstring(source)
        ET.indent(root, space="\t")
        formatted = ET.tostring(root, encoding="unicode")
        self.assertTrue(claude.same_policy(formatted, source))
        for before, after in (
            ('name="Authorization"', 'name="api-key"'),
            ('== "scope with space"', '!= "scope with space"'),
            ('"scope with space"', '"scope  with space"'),
            ('"two  spaces"', '"two spaces"'),
            ('Bearer ', 'Bearer  '),
            (' {"code": "denied"} ', '{"code": "denied"}'),
        ):
            with self.subTest(change=(before, after)):
                self.assertIn(before, source)
                self.assertFalse(claude.same_policy(source.replace(before, after), source))
                self.assertTrue(claude.same_policy(formatted, source))
        reordered = ET.fromstring(source)
        when = reordered.find("./choose/when")
        when.append(when[0])
        del when[0]
        self.assertFalse(claude.same_policy(ET.tostring(reordered, encoding="unicode"), source))
        self.assertFalse(claude.same_policy("<set-body> </set-body>", "<set-body/>"))
        self.assertFalse(claude.same_policy(
            '<include-fragment fragment-id="a-fixture000000"/>',
            '<include-fragment fragment-id="b-fixture000000"/>',
        ))
        for unsafe in (
            "<!DOCTYPE fragment [<!ENTITY x 'text'>]><fragment>&x;</fragment>",
            "<fragment><?unreviewed processing?></fragment>",
            "<?unreviewed processing?><fragment/>", "<fragment>", 1,
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(EvidenceError):
                claude.same_policy(unsafe, "<fragment/>")

    def test_actual_route_and_disabled_transition_refuse_meaningful_policy_changes(self):
        b = self.binding
        auth_path = (False, b["sourceApimResourceId"] + "/policyFragments/claude_auth_v1-fixture000000")
        policy_path = (False, b["sourceApimResourceId"] + "/apis/openai/policies/policy")
        self.verify()
        original = self.reader.responses[auth_path]["properties"]["value"]
        for before, after in (
            ('timeout="10"', 'timeout="11"'),
            ('client-id="{{claude-uami-client}}"', 'client-id="other-client"'),
            ("lifetime &lt; 120", "lifetime &lt; 0"),
            ("claude_authentication_unavailable", "claude_authentication_allowed"),
            ("api://AzureADTokenExchange", "api://AzureADTokenExchangeChanged"),
        ):
            self.assertIn(before, original)
            self.reader.responses[auth_path]["properties"]["value"] = original.replace(before, after)
            with self.subTest(change=before), self.assertRaisesRegex(EvidenceError, "claude_route_fragment_changed"):
                claude.verify_routes(b, self.reader, enabled=True)
            self.reader.responses[auth_path]["properties"]["value"] = original
            claude.verify_routes(b, self.reader, enabled=True)
        original_policy = self.reader.responses[policy_path]["properties"]["value"]
        self.reader.responses[policy_path]["properties"]["value"] = original_policy.replace("claude_auth_v1-", "other_auth_v1-")
        with self.assertRaisesRegex(EvidenceError, "claude_route_fragment_id"):
            claude.verify_routes(b, self.reader, enabled=True)
        disabled = FixtureReader(b, enabled=False)
        disabled.responses[(False, b["sourceApimResourceId"] + "/namedValues")]["value"][0]["properties"]["value"] = "changed"
        old = disabled.responses[auth_path]["properties"]["value"]
        disabled.responses[auth_path]["properties"]["value"] = old.replace('code="503"', 'code="200"')
        with self.assertRaisesRegex(EvidenceError, "disable_old_binding"):
            claude.verify_binding_transition(b, disabled)
        disabled.responses[auth_path]["properties"]["value"] = old
        claude.verify_binding_transition(b, disabled)

    def test_isolated_transport_pins_every_request_and_never_retries_failed_evidence(self):
        calls = []

        def runner(command, timeout, limit, *, extra_env):
            calls.append((command, timeout, limit, extra_env))
            return ProcessResult(b'{"value":[]}', False)

        with tempfile.TemporaryDirectory() as tmp, patch.object(claude, "az_command", return_value=["offline-az"]):
            reader = claude.Reader(self.binding, tmp, runner=runner)
            reader.get(True, self.binding.account_id, "2025-04-01-preview")
            reader.get(False, self.binding["sourceApimResourceId"], "2024-05-01")
            self.assertEqual(calls[0][3], {"AZURE_CONFIG_DIR": str(Path(tmp).resolve())})
            self.assertEqual(calls[1][3], {"AZURE_CONFIG_DIR": reader.source_config_dir})
            for target, call in zip((True, False), calls, strict=True):
                command, timeout, limit, _ = call
                self.assertEqual(command[command.index("--subscription") + 1], self.binding["targetSubscriptionId" if target else "sourceSubscriptionId"])
                self.assertNotIn("--tenant", command)
                self.assertIn("GET", command)
                self.assertLessEqual(timeout, 20)
                self.assertLessEqual(limit, 1024 * 1024)
            reader.get(False, self.binding["sourceApimResourceId"] + "/apis/openai/policies/policy", "2024-05-01")
            self.assertIn("&format=rawxml", calls[-1][0][calls[-1][0].index("--url") + 1])
            reader.runner = lambda *a, **kw: (_ for _ in ()).throw(EvidenceError("source_unavailable"))
            with self.assertRaisesRegex(EvidenceError, "source_unavailable"):
                reader.get(True, self.binding.account_id, "2025-04-01-preview")
            self.assertEqual(reader.calls, 4)

    def test_empty_target_plan_uses_exact_version_usage_name_not_quota_replicas(self):
        from datetime import UTC, datetime, timedelta
        from urllib.parse import parse_qs, urlsplit

        b = self.binding
        expected = claude.requirements(models())
        date = (datetime.now(UTC) + timedelta(days=100)).isoformat()
        rows = []
        quotas = []
        for desired in expected:
            counter = "AIServices." + desired["sku"] + "." + desired["model"]["name"] + ".Azure"
            rows.append({"model": {
                **desired["model"], "lifecycleStatus": "GenerallyAvailable",
                "capabilities": {"hostedOn": "azure"}, "deprecation": {"inference": date},
                "skus": [{
                    "name": desired["sku"], "usageName": counter, "deprecationDate": date,
                    "capacity": {"maximum": 1000000},
                }],
            }})
            quotas.extend([
                {"name": {"value": counter}, "currentValue": 0, "limit": desired["capacity"]},
                {"name": {"value": counter.removesuffix(".Azure")}, "currentValue": 0, "limit": 9999},
            ])
        # Azure can repeat identical offerings. Merge SKU rows only for the
        # synthetic per-model source envelope, as the real model list does.
        offers = {}
        for row in rows:
            name = row["model"]["name"]
            if name in offers:
                offers[name]["model"]["skus"].extend(row["model"]["skus"])
            else:
                offers[name] = row
        base = f"/subscriptions/{b['targetSubscriptionId']}/providers/Microsoft.CognitiveServices"
        group = b.account_id.rsplit("/providers/", 1)[0]
        responses = {
            group + "/providers/Microsoft.CognitiveServices/accounts": {"value": []},
            base + "/locations/eastus2/models": {"value": list(offers.values()) * 2},
            base + "/locations/eastus2/usages": {"value": quotas},
        }
        self.reader.responses.update({(True, key): value for key, value in responses.items()})
        command = self.reader.command

        def capacity_read(target, args):
            if args == ["account", "show"]:
                return command(target, args)
            self.assertTrue(target)
            query = parse_qs(urlsplit(args[-1]).query)
            selected = [row for row in expected if row["model"]["name"] == query["modelName"][0]]
            return {"value": [
                {"location": region, "properties": {
                    "model": row["model"], "skuName": row["sku"],
                    "availableCapacity": row["capacity"] if region == "eastus2" else 9999,
                }}
                for row in selected for region in ("eastus2", "swedencentral")
            ]}

        self.reader.command = capacity_read
        self.assertEqual(claude.target_preflight(b, models(), self.reader), 5)
        actual_quota = quotas[0]["limit"]
        quotas[0]["limit"] = 0
        with self.assertRaisesRegex(EvidenceError, "claude_raw_quota_insufficient"):
            claude.target_preflight(b, models(), self.reader)
        quotas[0]["limit"] = actual_quota
        self.assertEqual(claude.target_preflight(b, models(), self.reader), 5)
        responses[group + "/providers/Microsoft.CognitiveServices/accounts"]["value"] = [{"name": "learning"}]
        with self.assertRaisesRegex(EvidenceError, "claude_target_group_not_empty"):
            claude.target_preflight(b, models(), self.reader)

    def test_same_transport_refuses_warnings_and_continuation(self):
        for body, warning in ((b'{"value":[]}', True), (b'{"value":[],"nextLink":"https://other.invalid"}', False)):
            with tempfile.TemporaryDirectory() as tmp, patch.object(claude, "az_command", return_value=["offline-az"]):
                reader = claude.Reader(self.binding, tmp, runner=lambda *a, **kw: ProcessResult(body, warning))
                with self.assertRaises(EvidenceError):
                    reader.get(True, self.binding.account_id, "2025-04-01-preview")

    def test_source_and_external_catalog_denominators_are_explicit(self):
        document = models()
        all_rows = AVAILABILITY.catalog_requirements(document)
        source = AVAILABILITY.catalog_requirements(document, target="source")
        external = AVAILABILITY.catalog_requirements(document, target="external-claude")
        self.assertEqual(sum(map(len, all_rows.values())), sum(map(len, source.values())) + 5)
        self.assertEqual(sum(map(len, external.values())), 5)
        self.assertTrue(all(row["format"] != "Anthropic" for rows in source.values() for row in rows))
        self.assertEqual(
            [(r["model"]["name"], r["model"]["version"], r["sku"], r["capacity"]) for r in claude.requirements(document)],
            [("claude-opus-5", "2", "DataZoneStandard", 40), ("claude-sonnet-5", "2", "GlobalStandard", 80),
             ("claude-sonnet-5", "2", "DataZoneStandard", 80), ("claude-opus-5-5", "2", "GlobalStandard", 40),
             ("claude-opus-5-5", "2", "DataZoneStandard", 40)],
        )
        self.assertIn("sora-2", {model["name"] for model in source_catalog(document)["catalog"]})
        for field in ("deploymentTarget", "anthropicThinking", "samplingSupported", "reasoningEffort"):
            changed = deepcopy(document)
            row = next(m for m in changed["catalog"] if m.get("deploymentTarget") == "external-claude")
            del row[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                GENERATOR.build_catalog(changed)
        self.assertEqual(len(GENERATOR.build_catalog(document)["models"]), len(document["catalog"]))

    def test_adaptive_rows_are_text_only_in_every_catalog_validator(self):
        validator = load_script("claude_validate_catalog", ROOT / "scripts" / "validate-catalog.py")
        document = models()
        adaptive = next(m for m in document["catalog"] if m.get("anthropicThinking") == "adaptive")
        self.assertEqual((adaptive["toolCalling"], adaptive["inputModalities"]), (False, ["text"]))

        def validate(changed):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "models.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with patch.object(validator, "MODELS", path), patch("sys.stdout"):
                    return validator.main()

        # Controls: the shipped catalog passes both validators unchanged.
        self.assertEqual(validate(document), 0)
        self.assertEqual(len(GENERATOR.build_catalog(document)["models"]), len(document["catalog"]))
        for name, field, value in (
            ("claude-opus-5-5", "toolCalling", True),
            ("claude-opus-5-5", "toolCalling", None),
            ("claude-opus-5-5", "inputModalities", ["text", "image"]),
            ("claude-opus-5-5", "inputModalities", None),
            ("claude-opus-5-5", "reasoningEffort", ["low", "xhigh"]),
            ("claude-opus-5-5", "samplingSupported", True),
            ("claude-opus-5-5", "anthropicThinking", "enabled"),
        ):
            changed = deepcopy(document)
            row = next(m for m in changed["catalog"] if m["name"] == name)
            if value is None:
                del row[field]
            else:
                row[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    GENERATOR.build_catalog(changed)
                self.assertEqual(validate(changed), 1)
        # The disabled rows keep their unchanged rules: no text-only constraint applies.
        changed = deepcopy(document)
        next(m for m in changed["catalog"] if m["name"] == "claude-opus-5")["inputModalities"] = ["text", "image"]
        self.assertEqual(len(GENERATOR.build_catalog(changed)["models"]), len(document["catalog"]))
        # validate-catalog also checks a profile outside external-claude, where
        # model_target cannot see it; the paired control is the same row text-only.
        for tool_calling, expected in ((False, 0), (True, 1)):
            changed = deepcopy(document)
            shadow = deepcopy(adaptive)
            shadow.update(name="adaptive-profile-shadow", format="Anthropic-compatible", toolCalling=tool_calling)
            del shadow["deploymentTarget"]
            changed["catalog"].append(shadow)
            with self.subTest(shadow_tool_calling=tool_calling):
                self.assertEqual(validate(changed), expected)

    def test_source_retirement_cannot_borrow_offer_or_inventory_for_external_rows(self):
        from datetime import UTC, datetime

        rows = AVAILABILITY.catalog_requirements(models(), target="external-claude")["eastus2"]
        observations = AVAILABILITY.retirement_observations(
            rows, [], {}, region="eastus2", now=datetime.now(UTC),
            inventory_state="observed", inventory_observed_at=datetime.now(UTC),
        )
        self.assertEqual(len(observations), 5)
        self.assertTrue(all(row.incomplete and row.decision == "unknown" for row in observations))


if __name__ == "__main__":
    unittest.main()
