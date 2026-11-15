import base64
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import time
import unittest
from unittest import mock

import requests
from urllib3.response import HTTPResponse

HERE = Path(__file__).resolve().parent
SOURCE = HERE if (HERE / "hosted_oidc_read.py").exists() else HERE.parent
spec = importlib.util.spec_from_file_location("hosted_oidc_read", SOURCE / "hosted_oidc_read.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def fixture_env():
    return {
        "AI4IA_HOSTED_WEB_READ_ENABLED": "true",
        "AI4IA_HOSTED_WEB_READ_ADMIT": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": reader.REPOSITORY,
        "GITHUB_SHA": "a" * 40,
        "AI4IA_REVIEWED_SHA": "a" * 40,
        "AZURE_TENANT_ID": reader.TENANT,
        "AZURE_SUBSCRIPTION_ID": reader.SUBSCRIPTION,
        "AZURE_CLIENT_ID": reader.CLIENT,
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.actions.githubusercontent.com/token?request=synthetic",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-runtime-token",
    }


def assertion(**changes):
    claims = {
        "iss": "https://token.actions.githubusercontent.com",
        "sub": reader.SUBJECT,
        "aud": reader.AUDIENCE,
        "repository": reader.REPOSITORY,
        "ref": "refs/heads/main",
        "sha": "a" * 40,
        "environment": "production",
        "event_name": "workflow_dispatch",
        "nbf": int(time.time()) - 1,
        "exp": int(time.time()) + 600,
    }
    claims.update(changes)
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "synthetic." + encoded + ".signature"


def response(status, content, content_encoding=None):
    if not isinstance(content, bytes):
        content = json.dumps(content).encode()
    if content_encoding == "gzip":
        content = gzip.compress(content)
    value = requests.Response()
    value.status_code = status
    value.headers["Content-Length"] = str(len(content))
    if content_encoding:
        value.headers["Content-Encoding"] = content_encoding
    raw_headers = {"Content-Length": str(len(content))}
    if content_encoding:
        raw_headers["Content-Encoding"] = content_encoding
    value.raw = HTTPResponse(body=io.BytesIO(content), headers=raw_headers,
                             preload_content=False, status=status)
    value.url = "https://synthetic.invalid/"
    return value


class FakeNetwork:
    def __init__(self):
        self.calls = []
        self.assertion = assertion()
        self.token_status = 200
        self.arm_status = 200
        self.arm_body = {"id": reader.RESOURCE, "name": "ca-web-slurmfactory",
                         "type": "Microsoft.App/containerApps"}
        self.oidc_status = 200
        self.oversized = False

    def request(self, session, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.startswith((
            "https://pipelines.actions.githubusercontent.com/",
            "https://pipelinesghubeus2.actions.githubusercontent.com/",
        )):
            return response(self.oidc_status, {"value": self.assertion})
        if method == "POST" and url == reader.TOKEN_URL:
            return response(self.token_status, {"access_token": "synthetic-access-secret",
                                               "token_type": "Bearer", "expires_in": 3600})
        if method == "GET" and url == reader.ARM_URL:
            body = b"x" * (reader.MAX_BODY + 1) if self.oversized else self.arm_body
            return response(self.arm_status, body)
        raise AssertionError("unexpected outbound request")


class HostedReadTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNetwork()
        self.env = fixture_env()
        self.patches = [
            mock.patch.object(requests.Session, "request", autospec=True, side_effect=self.fake.request),
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network denied")),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_read(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            output = reader.execute(self.env)
        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(set(output), {"phase", "status", "sha256"})
        self.assertNotIn("synthetic-access-secret", json.dumps(output))
        self.assertNotIn("synthetic-runtime-token", json.dumps(output))
        return output

    def test_exact_success_with_real_installed_sdk_and_one_send_per_endpoint(self):
        output = self.run_read()
        expected = hashlib.sha256(json.dumps(self.fake.arm_body).encode()).hexdigest()
        self.assertEqual(output, reader.result("complete", 200, expected))
        self.assertEqual([x[0] for x in self.fake.calls], ["GET", "POST", "GET"])
        self.assertEqual(self.fake.calls[-1][1], reader.ARM_URL)
        for _, _, options in self.fake.calls:
            self.assertFalse(options["allow_redirects"])
            self.assertTrue(options["verify"])
            self.assertEqual(options["timeout"], (5, 5))
            self.assertEqual(options["headers"]["Accept-Encoding"], "identity")

    def test_runner_selected_actions_host_preserves_exact_assertion_binding(self):
        self.env["ACTIONS_ID_TOKEN_REQUEST_URL"] = (
            "https://pipelinesghubeus2.actions.githubusercontent.com/token?request=synthetic"
        )
        self.assertEqual(self.run_read()["phase"], "complete")
        self.assertEqual([x[0] for x in self.fake.calls], ["GET", "POST", "GET"])
        self.assertEqual(
            self.fake.calls[0][1],
            self.env["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=api%3A%2F%2FAzureADTokenExchange",
        )
        self.fake.calls.clear()
        self.fake.assertion = assertion(sub="repo:attacker/repo:environment:production")
        self.assertEqual(self.run_read()["phase"], "assertion-claims")
        self.assertEqual(len(self.fake.calls), 1)

    def test_oidc_host_boundary_refuses_deceptive_and_malformed_origins(self):
        for url in [
            "https://actions.githubusercontent.com/token",
            "https://pipelines.actions.githubusercontent.com.attacker.invalid/token",
            "https://pipelines-actions.githubusercontent.com/token",
            "https://a.b.actions.githubusercontent.com/token",
            "https://-bad.actions.githubusercontent.com/token",
            "https://bad-.actions.githubusercontent.com/token",
            "https://bad_name.actions.githubusercontent.com/token",
            "https://pipelinesghubeus2.actions.githubusercontent.com:444/token",
            "https://user@pipelinesghubeus2.actions.githubusercontent.com/token",
            "https://pipelinesghubeus2.actions.githubusercontent.com/token#fragment",
            "https://pipe\nlines.actions.githubusercontent.com/token",
        ]:
            with self.subTest(url=url), self.assertRaises(reader.Refused):
                reader.oidc_url(url)
        self.assertEqual(self.fake.calls, [])

    def test_admission_refuses_without_constructing_credential(self):
        for key, value in [("AI4IA_HOSTED_WEB_READ_ENABLED", "false"),
                           ("AI4IA_HOSTED_WEB_READ_ADMIT", "false"),
                           ("GITHUB_REF", "refs/heads/other"),
                           ("GITHUB_SHA", "b" * 40),
                           ("AZURE_CLIENT_ID", "bad"),
                           ("AZURE_TENANT_ID", "bad"),
                           ("AZURE_SUBSCRIPTION_ID", "bad")]:
            env = dict(self.env, **{key: value})
            with self.subTest(key=key), self.assertRaises(reader.Refused) as exc:
                reader.execute(env, credential_class=mock.Mock(side_effect=AssertionError("constructed")))
            self.assertEqual(exc.exception.phase, "admission")
        self.assertEqual(self.fake.calls, [])

    def test_environment_proxy_tls_refuses_before_credential(self):
        for key in ["HTTPS_PROXY", "http_proxy", "NO_PROXY", "REQUESTS_CA_BUNDLE",
                    "SSL_CERT_FILE", "SSLKEYLOGFILE", "AZURE_AUTHORITY_HOST"]:
            with self.subTest(key=key), self.assertRaises(reader.Refused) as exc:
                reader.execute(dict(self.env, **{key: "synthetic"}))
            self.assertEqual(exc.exception.phase, "environment")
        self.assertEqual(self.fake.calls, [])

    def test_wrong_oidc_url_or_claim_never_reaches_token_endpoint(self):
        for url in ["https://attacker.invalid/token", "http://pipelines.actions.githubusercontent.com/x",
                    "https://user@pipelines.actions.githubusercontent.com/x",
                    "https://pipelines.actions.githubusercontent.com/x?audience=bad"]:
            with self.subTest(url=url):
                self.env["ACTIONS_ID_TOKEN_REQUEST_URL"] = url
                self.assertEqual(self.run_read()["phase"], "assertion-url")
                self.assertEqual(self.fake.calls, [])
        self.env = fixture_env()
        self.fake.assertion = assertion(sub="repo:attacker/repo:environment:production")
        self.assertEqual(self.run_read()["phase"], "assertion-claims")
        self.assertEqual(len(self.fake.calls), 1)

    def test_assertion_redirect_refused_without_replay(self):
        self.fake.oidc_status = 302
        self.assertEqual(self.run_read(), reader.result("assertion-response", 302))
        self.assertEqual(len(self.fake.calls), 1)

    def test_assertion_refusals_distinguish_missing_token_from_transport(self):
        del self.env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        self.assertEqual(self.run_read(), reader.result("assertion-runtime-token"))
        self.assertEqual(self.fake.calls, [])
        self.env = fixture_env()
        with mock.patch.object(
            requests.Session, "request", side_effect=requests.Timeout("synthetic-private-runtime-url")
        ):
            self.assertEqual(self.run_read(), reader.result("assertion-transport"))
        self.assertEqual(self.fake.calls, [])

    def test_credential_failure_and_redirect_are_not_retried(self):
        for status in (401, 403, 302):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.fake.token_status = status
                self.assertEqual(self.run_read(), reader.result("credential", status))
                self.assertEqual(len(self.fake.calls), 2)

    def test_arm_unauthorized_redirect_no_retry(self):
        for status in (401, 403, 302):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.fake.arm_status = status
                self.assertEqual(self.run_read(), reader.result("arm", status))
                self.assertEqual(len(self.fake.calls), 3)

    def test_malformed_wrong_id_and_oversize_are_not_success(self):
        for body in (b"{", {"id": "wrong", "name": "ca-web-slurmfactory",
                            "type": "Microsoft.App/containerApps"}):
            with self.subTest(body=body):
                self.fake.calls.clear()
                self.fake.arm_body = body
                self.assertEqual(self.run_read()["phase"], "response")
                self.assertEqual(len(self.fake.calls), 3)
        self.fake.calls.clear()
        self.fake.oversized = True
        self.assertEqual(self.run_read()["phase"], "arm")
        self.assertEqual(len(self.fake.calls), 3)

    def test_bounded_body_uses_identity_representation_only(self):
        body = b'{"value":"sample"}'
        identity = response(200, body)
        status, observed = reader._bounded(identity, 1024, "arm")
        self.assertEqual((status, observed), (200, body))
        compressed = response(200, body, content_encoding="gzip")
        self.assertGreater(int(compressed.headers["Content-Length"]), len(body))
        with mock.patch.object(compressed, "iter_content", wraps=compressed.iter_content) as iter_content:
            with self.assertRaises(reader.Refused) as exc:
                reader._bounded(compressed, 1024, "arm")
            iter_content.assert_not_called()
        self.assertEqual((exc.exception.phase, exc.exception.status), ("arm", 200))

    def test_credential_failure_text_is_not_exposed(self):
        class BadCredential:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("PRIVATE TOKEN AND URL")
        out = reader.execute(self.env, credential_class=BadCredential)
        self.assertEqual(out, reader.result("credential"))
        self.assertEqual(self.fake.calls, [])

    def test_claim_fields_are_bound_to_admission(self):
        for claim, value in [("aud", "bad"), ("sha", "b" * 40), ("ref", "refs/heads/dev"),
                             ("event_name", "push"), ("environment", "other"),
                             ("exp", int(time.time()) - 1)]:
            with self.subTest(claim=claim):
                self.fake.calls.clear()
                self.fake.assertion = assertion(**{claim: value})
                self.assertEqual(self.run_read()["phase"], "assertion-claims")
                self.assertEqual(len(self.fake.calls), 1)

    def test_transport_refuses_second_token_send(self):
        class FakeTransport:
            def __init__(self, *, session, session_owner):
                self.session = session

        class RetryCredential:
            def __init__(self, tenant, client, callback, *, transport, **kwargs):
                self.callback = callback
                self.transport = transport

            def get_token(self, scope):
                jwt = self.callback()
                data = {"client_assertion": jwt,
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        "client_id": reader.CLIENT, "grant_type": "client_credentials",
                        "scope": scope}
                self.transport.session.request("POST", reader.TOKEN_URL, data=data)
                self.transport.session.request("POST", reader.TOKEN_URL, data=data)
                raise AssertionError("second send must refuse")

            def close(self):
                pass

        out = reader.execute(self.env, credential_class=RetryCredential, transport_class=FakeTransport)
        self.assertEqual(out, reader.result("credential"))
        self.assertEqual(len(self.fake.calls), 2)

    def test_child_deadline_kills_only_owned_process(self):
        code, output, timed_out, overflow = reader._run_owned_child(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2,
        )
        self.assertTrue(timed_out)
        self.assertFalse(overflow)
        self.assertNotEqual(code, 0)
        self.assertEqual(output, b"")

    def test_child_output_limit_kills_owned_process_and_caps_capture(self):
        code, output, timed_out, overflow = reader._run_owned_child(
            [sys.executable, "-c", "import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(30)"],
            timeout=10, max_output=128,
        )
        self.assertFalse(timed_out)
        self.assertTrue(overflow)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(output), 129)

    def test_launch_failure_emits_only_fixed_carrier_result(self):
        with mock.patch.object(reader.sys, "argv", ["hosted_oidc_read.py"]), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(reader.subprocess, "Popen", side_effect=OSError("synthetic-private-launch")), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            code = reader.main()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), reader.result("carrier"))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("synthetic-private-launch", output.getvalue())

    def test_post_launch_failures_cleanup_before_fixed_carrier_result(self):
        for failing_step in ("start", "supervision"):
            with self.subTest(failing_step=failing_step):
                child = mock.Mock()
                child.returncode = None
                child.stdout = io.BytesIO()
                child.poll.return_value = None
                thread = mock.Mock()
                thread.ident = None if failing_step == "start" else 123
                thread.is_alive.return_value = False
                if failing_step == "start":
                    thread.start.side_effect = OSError("synthetic-private-thread")
                with mock.patch.object(reader.sys, "argv", ["hosted_oidc_read.py"]), \
                     mock.patch.dict(os.environ, self.env, clear=True), \
                     mock.patch.object(reader.subprocess, "Popen", return_value=child), \
                     mock.patch.object(reader.threading, "Thread", return_value=thread), \
                     mock.patch.object(reader.time, "sleep", side_effect=OSError("synthetic-private-supervisor")), \
                     contextlib.redirect_stdout(io.StringIO()) as output, \
                     contextlib.redirect_stderr(io.StringIO()) as errors:
                    code = reader.main()
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue()), reader.result("carrier"))
                self.assertEqual(errors.getvalue(), "")
                child.kill.assert_called_once_with()
                child.wait.assert_called_once_with(timeout=5)
                self.assertTrue(child.stdout.closed)
                if failing_step == "supervision":
                    thread.join.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
