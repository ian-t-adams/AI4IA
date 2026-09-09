"""Direct API status probes are bounded, anonymous and stronger than reachability."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPERS = REPO / "scripts" / "status-endpoints.ps1"


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class StatusEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("pwsh")
        if not cls.pwsh:
            raise RuntimeError("pwsh is required to exercise status-endpoints.ps1")

    def run_ps(self, body: str) -> object:
        result = subprocess.run(
            [
                self.pwsh, "-NoProfile", "-NonInteractive", "-Command",
                f"$ErrorActionPreference = 'Stop'; . {ps_quote(str(HELPERS))}\n{body}",
            ],
            capture_output=True, text=True, check=False, timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_target_discovery_is_exact_public_and_never_guesses(self) -> None:
        api = {
            "type": "microsoft.app/containerapps", "service": "api",
            "ingressFqdn": "api.example.test", "ingressExternal": "true",
        }
        cases = [
            {"resources": [api], "expected": "", "url": "https://api.example.test"},
            {"resources": [], "expected": "target_unresolved"},
            {"resources": [api, api], "expected": "target_ambiguous"},
            {"resources": [{**api, "service": "web"}], "expected": "target_unresolved"},
            {"resources": [{**api, "ingressExternal": "false"}], "expected": "private_ingress"},
            {"resources": [{**api, "ingressExternal": ""}], "expected": "private_ingress"},
            {"resources": [{**api, "ingressFqdn": ""}], "expected": "invalid_target"},
            {"resources": [{**api, "ingressFqdn": "user:secret@api.example.test"}],
             "expected": "invalid_target"},
            {"resources": [], "override": "https://override.example.test/",
             "expected": "", "url": "https://override.example.test"},
        ]
        for invalid in (
            "http://api.example.test", "https://user:secret@api.example.test",
            "https://api.example.test?token=secret", "https://api.example.test#secret",
            "https://api.example.test/health/live", "https://127.0.0.1",
            "https://[::1]", "https://localhost", "https://app.localhost",
            "https://api.example.test:8443", "not a URL",
        ):
            cases.append({"resources": [api], "override": invalid, "expected": "invalid_target"})
        results = self.run_ps(f"""
            $cases = ConvertFrom-Json -AsHashtable {ps_quote(json.dumps(cases))}
            $results = @(foreach ($case in $cases) {{
                Resolve-ApiStatusTarget -Url $case.override -Resources $case.resources
            }})
            ConvertTo-Json -InputObject $results -Depth 6
        """)
        for case, result in zip(cases, results, strict=True):
            with self.subTest(case=case):
                self.assertEqual(result["outcome"], case["expected"])
                self.assertEqual(result["url"], case.get("url", ""))
                self.assertNotIn("secret", json.dumps(result))

    def test_api_health_requires_the_exact_contract_and_never_publishes_content(self) -> None:
        cases = [
            ("liveness", 200, {"status": "ok"}, "up", "healthy"),
            ("readiness", 200, {"status": "ok", "stage": "session_store"}, "up", "healthy"),
            ("readiness", 503, {"status": "unavailable", "stage": "session_store"},
             "down", "persistence_unavailable"),
            ("liveness", 401, {}, "unknown", "auth_required"),
            ("readiness", 403, {}, "unknown", "auth_required"),
            ("readiness", 302, {}, "unknown", "unexpected_redirect"),
            ("readiness", 502, {}, "down", "http_error"),
            ("liveness", 503, {}, "down", "http_error"),
            ("readiness", 200, {"status": "ok"}, "unknown", "invalid_response"),
            ("readiness", 200, {"status": "ok", "stage": "gateway"}, "unknown", "invalid_response"),
            ("readiness", 200, {"status": "unavailable", "stage": "session_store"},
             "unknown", "invalid_response"),
            ("readiness", 503, {"status": "ok", "stage": "session_store"},
             "unknown", "invalid_response"),
            ("liveness", 200, {"status": ["ok"]}, "unknown", "invalid_response"),
            ("liveness", 200, {"status": "OK"}, "unknown", "invalid_response"),
            ("liveness", 200, [{"status": "ok"}], "unknown", "invalid_response"),
            ("liveness", 200, None, "unknown", "invalid_response"),
        ]
        payloads = [
            {"kind": kind, "httpStatus": code, "body": json.dumps(body),
             "bodyValid": True, "contentType": "application/json"}
            for kind, code, body, _, _ in cases
        ]
        for override in (
            {"body": "{private-response-secret"},
            {"bodyValid": False, "body": "private-response-secret"},
            {"contentType": "text/html", "body": "private-response-secret"},
            {"body": '{"nested":{"a":{"b":{"c":{"d":"private-response-secret"}}}}}'},
            {"body": "{'status':'ok'}"},
            {"body": '{status:"ok"}'},
            {"body": '{"status":"ok",}'},
            {"body": '{"status":"ok"/*comment*/}'},
            {"body": '{"status":"unavailable","status":"ok"}'},
        ):
            payloads.append({**payloads[0], **override})
            cases.append(("liveness", 200, None, "unknown", "invalid_response"))
        results = self.run_ps(f"""
            $cases = ConvertFrom-Json -AsHashtable {ps_quote(json.dumps(payloads))}
            $script:urls = @()
            function Invoke-ApiHealthRequest {{
                param([string] $Url)
                $script:urls += $Url
                return $script:response
            }}
            $results = @(foreach ($case in $cases) {{
                $script:response = $case
                Test-ApiHealthEndpoint -Kind $case.kind -Target @{{
                    url = 'https://api.example.test'; outcome = ''; note = ''
                }}
            }})
            @{{ results = $results; urls = $script:urls }} | ConvertTo-Json -Depth 6
        """)
        self.assertEqual(len(results["urls"]), len(cases))
        for case, result, url in zip(cases, results["results"], results["urls"], strict=True):
            kind, _, _, state, outcome = case
            with self.subTest(case=case):
                self.assertEqual(result["state"], state)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["ok"], state == "up")
                self.assertEqual(result["kind"], kind)
                self.assertTrue(result["observedAt"].endswith("Z"))
                self.assertGreaterEqual(result["latencyMs"], 0)
                self.assertEqual(url, "https://api.example.test/health/" +
                                 ("live" if kind == "liveness" else "ready"))
                self.assertNotIn("body", result)
        self.assertNotIn("private-response-secret", json.dumps(results))

    def test_network_failures_and_unobserved_targets_have_distinct_evidence(self) -> None:
        results = self.run_ps("""
            $script:calls = 0
            function Invoke-ApiHealthRequest {
                param([string] $Url)
                $script:calls++
                if ($script:calls -eq 1) {
                    throw [System.Net.Http.HttpRequestException]::new('private-network-secret')
                }
                throw [System.OperationCanceledException]::new('private-timeout-secret')
            }
            $results = @(
                (Test-ApiHealthEndpoint -Kind liveness -Target @{
                    url = 'https://api.example.test'; outcome = ''; note = ''
                }),
                (Test-ApiHealthEndpoint -Kind readiness -Target @{
                    url = 'https://api.example.test'; outcome = ''; note = ''
                }),
                (Test-ApiHealthEndpoint -Kind readiness -Target @{
                    url = ''; outcome = 'target_unresolved'; note = 'No target.'
                })
            )
            @{ results = $results; calls = $script:calls } | ConvertTo-Json -Depth 6
        """)
        self.assertEqual(results["calls"], 2)
        for result in results["results"][:2]:
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(result["outcome"], "network_unavailable")
            self.assertIsNotNone(result["observedAt"])
            self.assertGreaterEqual(result["latencyMs"], 0)
        unobserved = results["results"][2]
        self.assertEqual(unobserved["outcome"], "target_unresolved")
        self.assertIsNone(unobserved["observedAt"])
        self.assertIsNone(unobserved["latencyMs"])
        self.assertNotIn("private-", json.dumps(results))

    def test_real_transport_is_bounded_anonymous_and_does_not_follow_redirects(self) -> None:
        requests = []
        responses = {
            "/boundary": (200, "application/json", b"a" * 4096),
            "/oversized": (200, "application/json", b"a" * 4097),
            "/invalid-utf8": (200, "application/json", b"\xff"),
            "/html": (200, "text/html", b"private-html-secret"),
            "/redirect": (302, "text/plain", b""),
            "/must-not-follow": (200, "application/json", b'{"status":"ok"}'),
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                code, content_type, body = responses[self.path]
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                if code == 302:
                    self.send_header("Location", "/must-not-follow")
                    self.send_header("Set-Cookie", "private-cookie-secret=value")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                results = self.run_ps(f"""
                    $results = @(foreach ($path in 'boundary','oversized','invalid-utf8','html','redirect') {{
                        $response = Invoke-ApiHealthRequest -Url "http://127.0.0.1:{port}/$path"
                        @{{
                            status = $response.httpStatus; valid = $response.bodyValid
                            length = $response.body.Length
                        }}
                    }})
                    ConvertTo-Json -InputObject $results
                """)
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.assertEqual([request[0] for request in requests], [
            "/boundary", "/oversized", "/invalid-utf8", "/html", "/redirect",
        ])
        self.assertEqual(results[0], {"status": 200, "valid": True, "length": 4096})
        for result in results[1:]:
            self.assertFalse(result["valid"])
            self.assertEqual(result["length"], 0)
        self.assertEqual(results[-1]["status"], 302)
        for _, headers in requests:
            normalized = {key.lower(): value for key, value in headers.items()}
            self.assertNotIn("authorization", normalized)
            self.assertNotIn("cookie", normalized)
            self.assertEqual(normalized["accept"], "application/json")
            self.assertEqual(normalized["cache-control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
