"""Offline controls for the opt-in REST speech canary; no actual requests."""
from __future__ import annotations

import asyncio
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
canary = load_script("speech_canary", ROOT / "scripts/speech-canary.py", register=True)
URL = "https://api.example.test/api/voice/speech"
MODEL = "gpt-4o-mini-tts"
TOKEN = "synthetic.private.token"
ARGV = ["--url", URL, "--model", MODEL, "--region", "eastus2"]


def wav(*, frames=2400, rate=24000, channels=1, streaming=False):
    pcm = b"\1\0" * channels * frames
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16)
    data_size = 0xFFFFFFFF if streaming else len(pcm)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", data_size) + pcm
    return b"RIFF" + struct.pack("<I", 0xFFFFFFFF if streaming else len(body)) + body


class Chunks:
    def __init__(self, data, delay=0):
        self.data, self.delay, self.reads = data, delay, 0

    async def __aiter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        for offset in range(0, len(self.data), 8192):
            self.reads += 1
            yield self.data[offset:offset + 8192]

    def iter_chunked(self, size):
        if size != 8192:
            raise AssertionError("Unbounded or unexpected read size")
        return self


class FakeClientError(Exception):
    pass


class Response:
    def __init__(self, data=None, *, status=200, headers=None, delay=0, error=None):
        data = wav() if data is None else data
        self.status = status
        self.raw_headers = headers if headers is not None else (
            (b"Content-Type", b"audio/wav"),
            (b"Content-Length", str(len(data)).encode()),
            (b"X-Model", MODEL.encode()),
        )
        self.content = Chunks(data, delay)
        self.error = error
        self.closed = False

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        self.closed = True


class Session:
    def __init__(self, response, calls, **kwargs):
        self.response, self.calls, self.options = response, calls, kwargs
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class SpeechCanaryTests(unittest.TestCase):
    def setUp(self):
        self.target = canary.resolve_target(MODEL, "eastus2")

    def probe(self, response, *, timeout=1):
        calls, sessions = [], []
        def factory(**kwargs):
            session = Session(response, calls, **kwargs)
            sessions.append(session)
            return session
        module = SimpleNamespace(
            ClientSession=factory, ClientTimeout=lambda **kwargs: kwargs,
            DummyCookieJar=lambda: "no-cookies", ClientError=FakeClientError,
        )
        with patch.dict(sys.modules, {"aiohttp": module}):
            report = asyncio.run(canary.run_canary(URL, self.target, TOKEN, timeout))
        self.assertEqual(len(calls), 1)
        self.assertTrue(sessions[0].closed)
        self.assertNotIn(TOKEN, json.dumps(report))
        self.assertNotIn("input", report)
        return report, calls, sessions[0]

    def test_catalog_resolution_binds_tts_version_without_deployment_or_pool_guess(self):
        self.assertEqual(self.target.catalogVersion, "2025-12-15")
        self.assertRegex(self.target.catalogSha256, r"^[a-f0-9]{64}$")
        self.assertEqual(canary.resolve_target("tts-hd", "swedencentral").catalogVersion, "001")
        for model, region in (("gpt-realtime-1.5", "eastus2"), (MODEL, "swedencentral"),
                              ("unknown", "eastus2"), ("name?token=secret", "eastus2")):
            with self.subTest(model=model, region=region), self.assertRaises(canary.CanaryInputError):
                canary.resolve_target(model, region)

    def test_only_an_explicit_https_app_speech_url_is_accepted(self):
        self.assertEqual(canary.validate_url(URL), URL)
        for bad in (
            URL.replace("https:", "http:"), URL + "/", URL + "?token=secret",
            URL + "#fragment", URL.replace("api.example.test", "user:secret@api.example.test"),
            URL.replace("api.example.test", "127.0.0.1"),
            URL.replace("api.example.test", "api.localhost"),
            URL.replace("api.example.test", "api.example.test:99999"),
            URL.replace("api.example.test", "gateway.azure-api.net"),
            URL.replace("api.example.test", "model.openai.azure.com"),
            URL.replace("api.example.test", "model.services.ai.azure.com"),
            " " + URL, "\n" + URL, URL.replace("/voice/speech", "/voice/live"),
        ):
            with self.subTest(url=bad), self.assertRaises(canary.CanaryInputError):
                canary.validate_url(bad)

    def test_disabled_tts_is_not_an_executable_canary_target(self):
        model = {"name": MODEL, "category": "tts", "runtimeEnabled": False,
                 "deployments": [{"region": "eastus2", "version": "2025-12-15"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            with patch.object(canary, "CATALOG", path):
                path.write_text(json.dumps({"catalog": [model]}), encoding="utf-8")
                with self.assertRaises(canary.CanaryInputError):
                    canary.resolve_target(MODEL, "eastus2")
                model["runtimeEnabled"] = True
                path.write_text(json.dumps({"catalog": [model]}), encoding="utf-8")
                self.assertEqual(canary.resolve_target(MODEL, "eastus2").model, MODEL)

    def test_execute_is_required_before_any_worker_or_token_read(self):
        success = canary.result(self.target, "success", http_status=200, audio=canary.inspect_wav(wav()))
        get = canary.os.environ.get
        def no_credential_read(key, default=None):
            if key == canary.TOKEN_ENV:
                raise AssertionError("credential read")
            return get(key, default)
        with patch.object(canary, "run_bounded", return_value=success) as worker:
            with patch.object(canary.os.environ, "get", side_effect=no_credential_read):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(canary.main(ARGV), 2)
                self.assertEqual(json.loads(output.getvalue())["outcome"], "not_run")
                worker.assert_not_called()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(canary.main([*ARGV, "--execute"]), 0)
            self.assertEqual(json.loads(output.getvalue())["outcome"], "success")
            worker.assert_called_once()

    def test_request_uses_app_auth_and_fixed_synthetic_text_without_redirects_or_ambient_auth(self):
        response = Response()
        report, calls, session = self.probe(response)
        self.assertEqual(report["outcome"], "success")
        self.assertEqual(report["modelVersionEvidence"], "declared-only")
        self.assertEqual(report["audio"], {
            "bytes": len(wav()), "channels": 1, "sampleRateHz": 24000,
            "frames": 2400, "durationMs": 100,
        })
        self.assertTrue(response.closed)
        args, request = calls[0]
        self.assertEqual(args, (URL,))
        self.assertEqual(request["json"], {
            "input": canary.SYNTHETIC_TEXT, "model": MODEL, "region": "eastus2",
            "voice": "alloy", "format": "wav",
        })
        self.assertEqual(request["headers"], {
            "Authorization": f"Bearer {TOKEN}", "Accept": "audio/wav",
            "Accept-Encoding": "identity", "Cache-Control": "no-store",
        })
        self.assertIs(request["allow_redirects"], False)
        self.assertNotIn("ssl", request)  # No TLS bypass.
        self.assertEqual(session.options, {
            "timeout": {"total": 1}, "trust_env": False, "auto_decompress": False,
            "cookie_jar": "no-cookies", "max_line_size": 8192,
            "max_field_size": 8192, "read_bufsize": 8192,
        })

    def test_bad_status_or_headers_never_read_a_body_and_valid_control_does(self):
        for status in (302, 401, 403, 429, 500):
            response = Response(status=status)
            report, _, _ = self.probe(response)
            self.assertEqual(report["outcome"], "http_error")
            self.assertEqual(response.content.reads, 0)
        headers = [(b"Content-Type", b"audio/wav"), (b"X-Model", MODEL.encode())]
        invalid = [
            [(b"Content-Type", b"application/json"), headers[1]], headers[:1],
            [*headers, (b"Content-Encoding", b"gzip")],
            [*headers, (b"Content-Length", b"1000001")],
            [*headers, (b"Content-Length", b"0")],
            [*headers, (b"Content-Length", b"+44")],
            [*headers, (b"Content-Type", b"audio/wav")],
            [*headers, (b"Content-Length", b"44"), (b"Content-Length", b"45")],
            [*headers, (b"Untrusted", b"x" * 8192)],
            [*headers, *[(b"X-Untrusted", b"x")] * 65],
        ]
        for raw in invalid:
            response = Response(headers=raw)
            report, _, _ = self.probe(response)
            self.assertEqual(report["outcome"], "invalid_headers")
            self.assertEqual(response.content.reads, 0)
        response = Response(headers=headers)
        self.assertEqual(self.probe(response)[0]["outcome"], "success")
        self.assertGreater(response.content.reads, 0)

    def test_audio_read_bound_and_declared_length_mismatch(self):
        headers = [(b"Content-Type", b"audio/wav"), (b"X-Model", MODEL.encode())]
        full = wav(channels=2, frames=(canary.MAX_AUDIO_BYTES - 44) // 4)
        self.assertEqual(len(full), canary.MAX_AUDIO_BYTES)
        self.assertEqual(self.probe(Response(full, headers=headers))[0]["outcome"], "success")
        self.assertEqual(
            self.probe(Response(full + b"x", headers=headers))[0]["outcome"], "oversized_audio",
        )
        self.assertEqual(self.probe(Response(
            headers=[*headers, (b"Content-Length", b"44")],
        ))[0]["outcome"], "invalid_audio")

    def test_wav_structure_duration_and_streaming_sentinel_controls(self):
        self.assertEqual(canary.inspect_wav(wav(streaming=True))["frames"], 2400)
        self.assertEqual(canary.inspect_wav(wav(frames=120000, rate=8000))["durationMs"], 15000)
        for bad in (
            b'{"error":"not audio"}', b"RIFF\0\0\0\0WAVEfake",
            wav()[:-1], wav() + b"\0", wav(frames=0), wav(frames=2399),
            wav(frames=120001, rate=8000), wav(channels=3),
            wav()[:20] + struct.pack("<H", 3) + wav()[22:],
            wav()[:32] + struct.pack("<H", 100) + wav()[34:],
            wav()[:12] + b"JUNK" + struct.pack("<I", 0xFFFFFFFF) + wav()[12:],
        ):
            with self.subTest(length=len(bad)), self.assertRaises(canary.CanaryResponseError):
                canary.inspect_wav(bad)

    def test_total_async_deadline_and_transport_errors_are_content_free(self):
        report, _, _ = self.probe(Response(delay=0.1), timeout=0.01)
        self.assertEqual(report["outcome"], "timeout")
        report, _, _ = self.probe(Response(error=FakeClientError(f"Authorization: Bearer {TOKEN}")))
        self.assertEqual(report["outcome"], "connection_error")

    def test_worker_deadline_has_no_retry_or_token_in_arguments(self):
        args = SimpleNamespace(url=URL, token_env=canary.TOKEN_ENV, timeout=30)
        with patch.object(canary.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 35)) as run:
            report = canary.run_bounded(args, self.target)
        self.assertEqual(report["outcome"], "timeout")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 35)
        self.assertNotIn(TOKEN, str(run.call_args))
        self.assertIn("--expected-catalog-sha256", run.call_args.args[0])

    def test_worker_result_must_be_bounded_and_bind_the_same_declared_target(self):
        args = SimpleNamespace(url=URL, token_env=canary.TOKEN_ENV, timeout=30)
        valid = canary.result(self.target, "success", http_status=200, audio=canary.inspect_wav(wav()))
        for change in (
            {"target": {}}, {"outcome": []}, {"httpStatus": 302},
            {"modelVersionEvidence": "live-verified"}, {"audio": None},
            {"audio": {**valid["audio"], "frames": -1}}, {"secret": TOKEN},
        ):
            invalid = {**valid, **change}
            completed = SimpleNamespace(stdout=json.dumps(invalid).encode(), returncode=0)
            with patch.object(canary.subprocess, "run", return_value=completed):
                report = canary.run_bounded(args, self.target)
            self.assertEqual(report["outcome"], "worker_error")
            self.assertNotIn(TOKEN, json.dumps(report))
        completed = SimpleNamespace(stdout=json.dumps(valid).encode(), returncode=0)
        with patch.object(canary.subprocess, "run", return_value=completed):
            self.assertEqual(canary.run_bounded(args, self.target), valid)

    def test_ci_runs_only_offline_speech_tests(self):
        quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
        self.assertIn("scripts.tests.test_speech_canary", quality)
        for workflow in (ROOT / ".github/workflows").glob("*.y*ml"):
            self.assertNotIn("speech-canary.py --execute", workflow.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
