"""Synthetic permission responses and OIDC only; no network or credentials."""

import json
import unittest
from pathlib import Path
from unittest import mock

from scripts.tests._loader import load_script
from scripts.tests.test_dispatch_gated_workflow import WRAPPER, FakeGh, run

READ = load_script("gateway_permissions", Path(__file__).resolve().parents[1]
                   / "hosted_gateway_permissions.py")
SHA = "c" * 40
ENV = {
    READ.GATE: "true", "AI4IA_GATEWAY_PERMISSIONS_READ_ADMIT": "true",
    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
    "GITHUB_REPOSITORY": READ.TRANSPORT.REPOSITORY,
    "AZURE_TENANT_ID": READ.TRANSPORT.TENANT,
    "AZURE_SUBSCRIPTION_ID": READ.TRANSPORT.SUBSCRIPTION,
    "AZURE_CLIENT_ID": READ.TRANSPORT.CLIENT,
    "GITHUB_SHA": SHA, "AI4IA_REVIEWED_SHA": SHA,
}
BODY = {"value": [{"actions": ["Microsoft.App/*"], "notActions": []}]}


class PermissionReadTests(unittest.TestCase):
    def execute(self, responses):
        calls = []

        def http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            response = responses[len(calls) - 1]
            if isinstance(response, Exception):
                raise response
            status, body = response
            return status, {}, json.dumps(body).encode()

        with mock.patch.object(READ.TRANSPORT, "acquire_arm_token", return_value="synthetic-token"):
            receipt = READ.execute(ENV, http=http, now=lambda: 12345)
        return receipt, calls

    def test_only_four_exact_permission_gets_and_projected_metadata(self):
        receipt, calls = self.execute([(200, BODY)] * 4)
        self.assertEqual(receipt["phase"], "complete")
        self.assertFalse(receipt["effective_write_access_proven"])
        self.assertEqual(
            [(method, url) for method, url, _ in calls],
            [("GET", READ.TRANSPORT.Arm.url(
                scope + "/providers/Microsoft.Authorization/permissions", api="2022-04-01"))
             for _, scope in READ.SCOPES],
        )
        self.assertEqual([r["name"] for r in receipt["scopes"]], [n for n, _ in READ.SCOPES])
        self.assertNotIn("synthetic-token", json.dumps(receipt))
        for _, _, kwargs in calls:
            self.assertEqual(kwargs["hosts"], {"management.azure.com"})
            self.assertEqual(kwargs["limit"], 32768)
            self.assertEqual(kwargs["timeout"], 10)
            self.assertNotIn("body", kwargs)

    def test_denied_transport_and_pagination_are_unknown_without_fallback(self):
        page = {**BODY, "nextLink": "https://example.invalid/do-not-follow"}
        receipt, calls = self.execute([
            (403, {"error": {"message": "synthetic-private-error"}}),
            OSError("synthetic-private-transport"), (200, page), (200, {"value": []}),
        ])
        self.assertEqual(len(calls), 4)
        self.assertEqual(receipt["phase"], "incomplete")
        self.assertEqual([r["observed"] for r in receipt["scopes"]], [False, False, False, True])
        self.assertEqual([r["permissions"] for r in receipt["scopes"]], [None, None, None, []])
        self.assertNotIn("synthetic-private", json.dumps(receipt))
        self.assertNotIn("example.invalid", json.dumps(receipt))

    def test_unknown_fields_and_malformed_permissions_are_not_printed(self):
        cases = [
            {"value": [{"actions": ["*"], "notActions": [], "secret": "synthetic"}]},
            {"value": [{"actions": ["secret=synthetic"], "notActions": []}]},
            {"value": [{"actions": "*", "notActions": []}]},
            {"value": [{"actions": ["*"]}]},
            {"value": None}, [], {"value": [BODY["value"][0]] * 33},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(READ.TRANSPORT.Refused):
                READ.permissions(json.dumps(payload).encode())

    def test_admission_refuses_before_auth_or_http(self):
        changes = [
            {READ.GATE: ""}, {"AI4IA_GATEWAY_PERMISSIONS_READ_ADMIT": "false"},
            {"GITHUB_REF": "refs/heads/feature"}, {"AZURE_CLIENT_ID": "different"},
            {"AZURE_TENANT_ID": "different"}, {"AI4IA_REVIEWED_SHA": "d" * 40},
            {"HTTPS_PROXY": "synthetic"}, {"SSLKEYLOGFILE": "synthetic"},
        ]
        for change in changes:
            with (self.subTest(change=change),
                  mock.patch.object(READ.TRANSPORT, "acquire_arm_token") as auth,
                  self.assertRaises(READ.TRANSPORT.Refused)):
                READ.execute({**ENV, **change}, http=mock.Mock())
            auth.assert_not_called()

    def test_auth_refusal_never_reads_resources(self):
        http = mock.Mock()
        with mock.patch.object(READ.TRANSPORT, "acquire_arm_token",
                               side_effect=READ.TRANSPORT.Refused("credential", 403)):
            receipt = READ.execute(ENV, http=http)
        http.assert_not_called()
        self.assertEqual(receipt["scopes"], [])
        self.assertEqual(receipt["phase"], "credential")
        self.assertEqual(receipt["status"], 403)

    def test_wrapper_owns_new_gate_and_other_dispatches_refuse_while_held(self):
        gh = FakeGh(polls=1)
        self.assertTrue(run(gh, "hosted-gateway-permissions-read.yml", SHA)["gate_absent"])
        self.assertEqual([c[2] for c in gh.calls if c[:2] == ["variable", "set"]], [READ.GATE])
        self.assertEqual(gh.variables, {})
        gh = FakeGh(variables={READ.GATE: "true"})
        with self.assertRaises(WRAPPER.DispatchError):
            run(gh)
        self.assertFalse(any(c[:2] == ["workflow", "run"] for c in gh.calls))


if __name__ == "__main__":
    unittest.main()
