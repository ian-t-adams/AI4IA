"""Gateway mode is explicit, protected, and never a federation fallback."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _claude_gateway as gateway
from _capacity_evidence import EvidenceError, ProcessResult
from _claude_binding import Reader, configured_binding
from scripts.tests._claude_fixture import FixtureReader, binding
from scripts.tests.test_feature_prereqs import REAL_PARAMETERS, _environment, _run

ROOT = Path(__file__).resolve().parents[2]


def environment():
    return {
        "AI4IA_CLAUDE_ENABLED": "true", "AI4IA_CLAUDE_EXTERNAL_ENABLED": "false",
        "AI4IA_CLAUDE_GATEWAY_ENABLED": "true",
        "AI4IA_CLAUDE_GATEWAY_URL": "https://claude-fixture.azure-api.net/claude",
        "AI4IA_CLAUDE_GATEWAY_API_KEY": "syntheticGatewayKey0123456789",
    }


class ClaudeGatewayTests(unittest.TestCase):
    def test_policy_get_negotiates_json_instead_of_the_clis_raw_xml_export(self):
        value = binding()
        apim = value["sourceApimResourceId"]
        observed = []

        def transport(command, timeout, limit, **kwargs):
            observed.append(command)
            body = (
                b'{"properties":{"value":"<policies/>"}}'
                if "--headers" in command and command[command.index("--headers") + 1] == "Accept=application/json"
                else b"\xef\xbb\xbf<policies/>"
            )
            return ProcessResult(body, False)

        with tempfile.TemporaryDirectory() as directory, patch.object(gateway, "az_command", return_value=["az"]), patch.object(
            gateway, "run_bounded", side_effect=transport,
        ):
            readers = (
                gateway.SourceRouteReader(apim, value["sourceSubscriptionId"]),
                Reader(value, directory, runner=transport),
            )
            for reader in readers:
                result = reader.get(False, apim + "/apis/openai/policies/policy", "2024-05-01")
                self.assertEqual(result, {"properties": {"value": "<policies/>"}})
        self.assertEqual(len(observed), 2)
        for command in observed:
            self.assertIn("format=rawxml", command[command.index("--url") + 1])
            self.assertIn("--subscription", command)

    def test_plan_time_gate_requires_the_secret_and_does_not_accept_provider_urls(self):
        env = {
            **environment(), "AI4IA_CLAUDE_ORGANIZATION_NAME": "",
            "AI4IA_CLAUDE_COUNTRY_CODE": "", "AI4IA_CLAUDE_INDUSTRY": "",
        }
        with _environment(**env):
            self.assertEqual(_run(REAL_PARAMETERS)[0], 0)
        for change in (
            {"AI4IA_CLAUDE_GATEWAY_API_KEY": ""},
            {"AI4IA_CLAUDE_GATEWAY_URL": "https://model.services.ai.azure.com/anthropic"},
            {"AI4IA_CLAUDE_EXTERNAL_ENABLED": "true"},
        ):
            with self.subTest(change=change), _environment(**{**env, **change}):
                code, stdout, stderr = _run(REAL_PARAMETERS)
                self.assertNotEqual(code, 0)
                self.assertNotIn(env["AI4IA_CLAUDE_GATEWAY_API_KEY"], stdout + stderr)
        with _environment(**env):
            self.assertEqual(_run(REAL_PARAMETERS)[0], 0)

    def test_key_mode_is_explicit_and_never_falls_back_to_federation(self):
        env = environment()
        result = gateway.configured_gateway(env)
        self.assertEqual(result.url, env["AI4IA_CLAUDE_GATEWAY_URL"])
        self.assertNotIn(env["AI4IA_CLAUDE_GATEWAY_API_KEY"], repr(result))
        self.assertIsNone(configured_binding(env))
        for name, value in (
            ("AI4IA_CLAUDE_GATEWAY_ENABLED", "yes"),
            ("AI4IA_CLAUDE_EXTERNAL_ENABLED", "true"),
            ("AI4IA_CLAUDE_GATEWAY_URL", "https://attacker.invalid/claude"),
            ("AI4IA_CLAUDE_GATEWAY_URL", "https://claude-fixture.azure-api.net/claude?subscription-key=secret"),
            ("AI4IA_CLAUDE_GATEWAY_URL", "http://claude-fixture.azure-api.net/claude"),
            ("AI4IA_CLAUDE_GATEWAY_API_KEY", ""),
            ("AI4IA_CLAUDE_GATEWAY_API_KEY", "credential\r\nInjected: header"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(EvidenceError):
                configured_binding({**env, name: value})
        self.assertIsNone(gateway.configured_gateway({}))
        self.assertIsNotNone(gateway.configured_gateway(env))

    def test_actual_policy_readback_refuses_wrong_routes_and_unprotected_key_metadata(self):
        value = binding()
        reader = FixtureReader(value)
        env = {**environment(), "AZURE_SUBSCRIPTION_ID": value["sourceSubscriptionId"]}
        apim = value["sourceApimResourceId"]
        fragment = (False, apim + "/policyFragments/claude_auth_v1-fixture000000")
        reader.responses[fragment]["properties"]["value"] = (
            ROOT / "infra" / "policies" / "claude-gateway-auth.xml"
        ).read_text(encoding="utf-8")
        named = (False, apim + "/namedValues")
        reader.responses[named]["value"] = [
            {"name": name, "properties": {"value": text, "secret": False}}
            for name, text in {
                "claude-gateway-url": env["AI4IA_CLAUDE_GATEWAY_URL"],
                "claude-target-endpoint": env["AI4IA_CLAUDE_GATEWAY_URL"],
                "claude-proxy-subscription": "ai4ia-proxy-models",
            }.items()
        ] + [{"name": "claude-gateway-key", "properties": {"secret": True}}]
        with patch.object(gateway, "SourceRouteReader", return_value=reader):
            gateway.verify_gateway_routes(apim, env)
            good = deepcopy(reader.responses[named])
            reader.responses[named]["value"][-1]["properties"]["secret"] = False
            with self.assertRaisesRegex(EvidenceError, "secret_not_protected"):
                gateway.verify_gateway_routes(apim, env)
            reader.responses[named] = good
            original = reader.responses[fragment]
            reader.responses[fragment] = {"properties": {"value": "<fragment/>"}}
            with self.assertRaisesRegex(EvidenceError, "route_fragment_changed"):
                gateway.verify_gateway_routes(apim, env)
            reader.responses[fragment] = original
            gateway.verify_gateway_routes(apim, env)
        self.assertGreater(len(reader.calls), 20)

    def test_new_gateway_binding_must_be_staged_while_the_old_route_is_disabled(self):
        value = binding()
        reader = FixtureReader(value, enabled=False)
        env = {**environment(), "AZURE_SUBSCRIPTION_ID": value["sourceSubscriptionId"],
               "AI4IA_CLAUDE_ENABLED": "false"}
        apim = value["sourceApimResourceId"]
        key = (False, apim + "/policyFragments/claude_auth_v1-fixture000000")
        with patch.object(gateway, "SourceRouteReader", return_value=reader):
            gateway.verify_gateway_transition(apim, env)
            with self.assertRaisesRegex(EvidenceError, "initial_disabled_stage"):
                gateway.verify_gateway_transition(apim, {**env, "AI4IA_CLAUDE_ENABLED": "true"})
            disabled = reader.responses[key]
            reader.responses[key] = {"properties": {"value": (
                ROOT / "infra" / "policies" / "claude-federated-auth.xml"
            ).read_text(encoding="utf-8")}}
            with self.assertRaisesRegex(EvidenceError, "disable_old_binding"):
                gateway.verify_gateway_transition(apim, env)
            reader.responses[key] = disabled
            gateway.verify_gateway_transition(apim, env)

    def test_read_transport_is_scoped_bounded_and_never_reads_secrets(self):
        apim = binding()["sourceApimResourceId"]
        subscription = binding()["sourceSubscriptionId"]
        reader = gateway.SourceRouteReader(apim, subscription)
        with patch.object(gateway, "az_command", return_value=["az"]), patch.object(
            gateway, "run_bounded", return_value=ProcessResult(b'{"value":[]}', False),
        ) as run:
            self.assertEqual(reader.get(False, apim + "/namedValues", "2024-05-01"), {"value": []})
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--subscription") + 1], subscription)
            self.assertIn("https://management.azure.com" + apim + "/namedValues?api-version=2024-05-01", argv)
            for target, path in ((True, apim + "/namedValues"), (False, apim + "/namedValues/key/listSecrets"),
                                 (False, "/subscriptions/other/secret")):
                with self.subTest(path=path), self.assertRaises(EvidenceError):
                    reader.get(target, path, "2024-05-01")
            self.assertEqual(run.call_count, 1)
            run.return_value = ProcessResult(json.dumps({"value": [], "nextLink": "more"}).encode(), False)
            with self.assertRaisesRegex(EvidenceError, "continuation"):
                reader.get(False, apim + "/namedValues", "2024-05-01")
            run.return_value = ProcessResult(b'{"value":[]}', False)
            self.assertEqual(reader.get(False, apim + "/namedValues", "2024-05-01"), {"value": []})
