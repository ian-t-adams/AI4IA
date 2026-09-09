"""Read-only base-index drift must not confuse unavailable or child manifests with current pins."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-base-image-drift.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("base_image_drift", SCRIPT)
assert SPEC and SPEC.loader
drift = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = drift
SPEC.loader.exec_module(drift)

OCI = "application/vnd.oci.image.index.v1+json"
DOCKER = "application/vnd.docker.distribution.manifest.list.v2+json"
CHILD = "application/vnd.oci.image.manifest.v1+json"


def index_document(media_type: str = OCI) -> dict:
    return {
        "schemaVersion": 2, "mediaType": media_type,
        "manifests": [
            {"mediaType": CHILD, "digest": "sha256:" + "a" * 64, "size": 100,
             "platform": {"os": "linux", "architecture": "amd64"}},
            {"mediaType": CHILD, "digest": "sha256:" + "b" * 64, "size": 110,
             "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ],
    }


def index_response(document: dict | None = None):
    document = document if document is not None else index_document()
    body = json.dumps(document, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return drift.HttpResult(200, {
        "content-type": document.get("mediaType", OCI),
        "docker-content-digest": digest,
    }, body)


def pin_for(response, name="node:22-alpine"):
    return drift.parse_pin(name + "@" + response.headers["docker-content-digest"])


class BaseImageDriftTests(unittest.TestCase):
    def test_owned_sources_match_existing_gate_and_repeated_stages_are_deduplicated(self):
        from scripts.tests.test_base_image_pins import _dockerfiles_built_by_ci

        paths = drift.tracked_dockerfiles(ROOT)
        self.assertEqual(set(paths), _dockerfiles_built_by_ci())
        before = {path: (ROOT / path).read_bytes() for path in paths}
        pins = drift.collect_pins(ROOT)
        self.assertEqual(len(pins), 4)
        node = next(pin for pin in pins if pin.repository == "library/node")
        self.assertEqual(len(node.sources), 3)
        self.assertTrue(all(source.startswith("app/web/Dockerfile:") for source in node.sources))
        self.assertEqual(before, {path: (ROOT / path).read_bytes() for path in paths})

    def test_stage_aliases_are_not_registry_images_and_bad_froms_are_not_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Dockerfile"
            reference = "node:22-alpine@sha256:" + "a" * 64
            path.write_text(
                f"FROM --platform=$BUILDPLATFORM {reference} AS builder\n"
                "FROM builder AS runner\n", encoding="utf-8",
            )
            self.assertEqual(drift.external_references(path), [(1, reference)])
            for bad in ("FROM\n", "FROM node:22 \\\n", "# No base\n"):
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(drift.BaseSourceError):
                    drift.external_references(path)

    def test_invalid_or_private_refs_never_turn_into_public_lookups(self):
        for reference in (
            "node:22", "node@sha256:" + "a" * 64, "$PRIVATE_BASE", "scratch",
            "ghcr.io/private/image:v1@sha256:" + "a" * 64,
            "user:private-secret@registry.example/image:v1@sha256:" + "a" * 64,
            "mcr.microsoft.com/../private:v1@sha256:" + "a" * 64,
            "mcr.microsoft.com/dotnet/sdk:v1?secret@sha256:" + "a" * 64,
            "mcr.microsoft.com.attacker.example/dotnet/sdk:v1@sha256:" + "a" * 64,
            "attacker.example/mcr.microsoft.com/dotnet/sdk:v1@sha256:" + "a" * 64,
            "docker.io.attacker.example/library/node:v1@sha256:" + "a" * 64,
            "attacker.example/docker.io/library/node:v1@sha256:" + "a" * 64,
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(drift.BaseSourceError) as error:
                    drift.parse_pin(reference)
                self.assertNotIn("private-secret", str(error.exception))
        normalized = drift.parse_pin("docker.io/library/node:22@sha256:" + "a" * 64)
        self.assertEqual(normalized.registry, "registry-1.docker.io")
        self.assertEqual(normalized.repository, "library/node")
        microsoft = drift.parse_pin("mcr.microsoft.com/dotnet/sdk:10.0@sha256:" + "a" * 64)
        self.assertEqual(microsoft.registry, "mcr.microsoft.com")
        self.assertEqual(microsoft.repository, "dotnet/sdk")

    def test_dockerfile_read_limit_has_a_real_boundary_control(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Dockerfile"
            reference = "node:22@sha256:" + "a" * 64
            from_line = f"FROM {reference}\n"
            body = "#" + "x" * (128 * 1024 - len(from_line) - 2) + "\n" + from_line
            path.write_bytes(body.encode())
            self.assertEqual(drift.external_references(path), [(2, reference)])
            path.write_bytes((body + " ").encode())
            with self.assertRaisesRegex(drift.BaseSourceError, "dockerfile_too_large"):
                drift.external_references(path)

    def test_explicit_localhost_registry_is_not_reinterpreted_as_a_hub_namespace(self):
        with self.assertRaisesRegex(drift.BaseSourceError, "unsupported_public_registry"):
            drift.parse_pin("localhost/private-team/base:v1@sha256:" + "a" * 64)
        response = index_response()
        pin = pin_for(response, name="docker.io/localhost/team/base:v1")
        getter = Mock(return_value=response)
        result = drift.observe(pin, getter)
        self.assertIsNone(result["error"])
        self.assertEqual(getter.call_args.args[0],
                         "https://registry-1.docker.io/v2/localhost/team/base/manifests/v1")

    def test_both_index_formats_match_pins_and_report_changed_pins(self):
        for media_type in (OCI, DOCKER):
            with self.subTest(media_type=media_type):
                response = index_response(index_document(media_type))
                pin = pin_for(response)
                getter = Mock(return_value=response)
                observation = drift.observe(pin, getter)
                self.assertIsNone(observation["error"])
                self.assertEqual(observation["observedDigest"], pin.digest)
                self.assertEqual(observation["platforms"], ["linux/amd64", "linux/arm64/v8"])
                current = drift.make_report([pin], Mock(return_value=observation))
                self.assertEqual(current["status"], "current")
                changed_pin = drift.replace(pin, digest="sha256:" + "f" * 64)
                changed = drift.make_report([changed_pin], Mock(return_value=observation))
                self.assertEqual(changed["status"], "changed")
                self.assertEqual(changed["bases"][0]["observedDigest"], pin.digest)
                url, headers, limit = getter.call_args.args
                self.assertEqual(url, "https://registry-1.docker.io/v2/library/node/manifests/22-alpine")
                self.assertNotIn("Authorization", headers)
                self.assertIn(OCI, headers["Accept"])
                self.assertIn(DOCKER, headers["Accept"])
                self.assertEqual(limit, drift.MAX_MANIFEST_BYTES)

    def test_unknown_never_hides_other_observed_drift_or_passes_empty_coverage(self):
        response = index_response()
        pin = drift.replace(pin_for(response), digest="sha256:" + "f" * 64)
        observed = drift.observe(pin, Mock(return_value=response))
        unavailable = drift.observe(pin, Mock(side_effect=OSError("private-network-secret")))
        outcomes = iter([observed, unavailable])
        report = drift.make_report([pin, pin], lambda _pin: next(outcomes))
        self.assertEqual(report["status"], "unknown")
        self.assertEqual([row["status"] for row in report["bases"]], ["changed", "unknown"])
        self.assertIsNone(report["bases"][1]["observedDigest"])
        self.assertNotIn("private-network-secret", json.dumps(report))
        self.assertEqual(drift.make_report([])["status"], "unknown")

    def test_anonymous_docker_challenge_is_scoped_and_never_published(self):
        response = index_response()
        pin = pin_for(response)
        challenge = (
            'Bearer realm="https://auth.docker.io/token",'
            'service="registry.docker.io",scope="repository:library/node:pull"'
        )
        getter = Mock(side_effect=[
            drift.HttpResult(401, {"www-authenticate": challenge}, b"private-server-body"),
            drift.HttpResult(200, {}, b'{"token":"anonymous-private-token"}'),
            response,
        ])
        observation = drift.observe(pin, getter)
        self.assertIsNone(observation["error"])
        self.assertEqual(getter.call_count, 3)
        self.assertEqual(getter.call_args_list[1].args[0],
                         "https://auth.docker.io/token?service=registry.docker.io&scope=repository%3Alibrary%2Fnode%3Apull")
        self.assertNotIn("Authorization", getter.call_args_list[1].args[1])
        self.assertEqual(getter.call_args_list[2].args[1]["Authorization"], "Bearer anonymous-private-token")
        self.assertNotIn("private", json.dumps(observation))
        for invalid in (
            challenge.replace("auth.docker.io", "attacker.example"),
            challenge.replace(":pull", ":pull,push"),
            challenge.replace("library/node", "other/private"),
            challenge + ',realm="https://auth.docker.io/token"',
            'Basic realm="private"',
        ):
            getter = Mock(return_value=drift.HttpResult(401, {"www-authenticate": invalid}, b"private"))
            result = drift.observe(pin, getter)
            self.assertIsNotNone(result["error"])
            self.assertEqual(getter.call_count, 1)
            self.assertNotIn("private", json.dumps(result))

    def test_platform_manifests_contradictions_and_invalid_shapes_stay_unknown(self):
        response = index_response()
        pin = pin_for(response)
        wrong_digest = drift.HttpResult(200, {
            **response.headers, "docker-content-digest": "sha256:" + "f" * 64,
        }, response.body)
        malformed = drift.HttpResult(200, response.headers, b'{"private-invalid-json":')
        child = drift.HttpResult(200, {"content-type": CHILD},
                                 b'{"schemaVersion":2,"mediaType":"private-platform-manifest"}')
        invalid = [wrong_digest, malformed, child,
                   drift.HttpResult(302, {"location": "https://attacker.example"}, b"private"),
                   drift.HttpResult(503, {}, b"private")]
        for change in (
            {"schemaVersion": 2.0},
            {"manifests": []},
            {"manifests": [{"private": "invalid"}]},
            {"mediaType": DOCKER},
        ):
            document = {**index_document(), **change}
            body = json.dumps(document).encode()
            invalid.append(drift.HttpResult(200, {"content-type": OCI}, body))
        for platform in (
            {"os": "unknown", "architecture": "unknown"},
            {"os": ["private"], "architecture": "arm64"},
            {"os": "linux", "architecture": "amd64"},
        ):
            document = index_document()
            document["manifests"][1]["platform"] = platform
            invalid.append(index_response(document))
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                getter = Mock(return_value=candidate)
                result = drift.observe(pin, getter)
                self.assertIsNotNone(result["error"])
                self.assertIsNone(result["observedDigest"])
                self.assertEqual(getter.call_count, 1)
                self.assertNotIn("private", json.dumps(result))
        no_header = drift.HttpResult(200, {"content-type": OCI}, response.body)
        result = drift.observe(pin, Mock(return_value=no_header))
        self.assertIsNone(result["error"])
        self.assertEqual(result["observedDigest"], pin.digest)

    def test_duplicate_json_fields_and_non_json_constants_are_not_accepted(self):
        response = index_response()
        pin = pin_for(response)
        for body in (
            response.body.replace(b'"schemaVersion":2', b'"schemaVersion":1,"schemaVersion":2'),
            response.body.replace(b'"schemaVersion":2', b'"unexpected":NaN,"schemaVersion":2'),
        ):
            result = drift.observe(pin, Mock(return_value=drift.HttpResult(200, {
                "content-type": OCI,
                "docker-content-digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            }, body)))
            self.assertEqual(result["error"], "invalid_metadata_json")
            self.assertIsNone(result["observedDigest"])
        control = drift.observe(pin, Mock(return_value=response))
        self.assertIsNone(control["error"])

    def test_unsupported_media_cannot_pass_even_with_an_index_shaped_body(self):
        for media_type in (CHILD, "application/json"):
            response = index_response(index_document(media_type))
            result = drift.observe(pin_for(response), Mock(return_value=response))
            self.assertEqual(result["error"], "index_response_required")
            self.assertIsNone(result["observedDigest"])
        response = index_response()
        self.assertIsNone(drift.observe(pin_for(response), Mock(return_value=response))["error"])

    def test_attestation_descriptors_do_not_inflate_platform_coverage(self):
        document = index_document()
        attestation = copy.deepcopy(document["manifests"][0])
        attestation["platform"] = {"os": "unknown", "architecture": "unknown"}
        document["manifests"].append(attestation)
        response = index_response(document)
        result = drift.observe(pin_for(response), Mock(return_value=response))
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["platforms"]), 2)
        document["manifests"] = [document["manifests"][0], attestation]
        response = index_response(document)
        result = drift.observe(pin_for(response), Mock(return_value=response))
        self.assertEqual(result["error"], "multi_platform_index_required")

    def test_malformed_extra_platform_is_not_hidden_by_two_other_valid_platforms(self):
        for platform in ([], None, {}, {"os": "linux"}, {"architecture": "amd64"},
                         {"os": "unknown", "architecture": []}):
            with self.subTest(platform=platform):
                document = index_document()
                extra = copy.deepcopy(document["manifests"][0])
                extra["platform"] = platform
                document["manifests"].append(extra)
                response = index_response(document)
                pin = pin_for(response)
                observation = drift.observe(pin, Mock(return_value=response))
                self.assertIsNotNone(observation["error"])
                self.assertIsNone(observation["observedDigest"])
                self.assertEqual(drift.make_report([pin], Mock(return_value=observation))["status"],
                                 "unknown")
        document = index_document()
        extra = copy.deepcopy(document["manifests"][0])
        del extra["platform"]
        document["manifests"].append(extra)
        response = index_response(document)
        self.assertIsNone(drift.observe(pin_for(response), Mock(return_value=response))["error"])

    def test_https_transport_caps_body_and_does_not_follow_redirects(self):
        message = Message()
        message["Content-Type"] = OCI
        response = Mock(status=302, headers=message)
        response.read.return_value = b"body"
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(drift.http.client, "HTTPSConnection", return_value=connection) as factory:
            result = drift.https_get("https://mcr.microsoft.com/v2/dotnet/sdk/manifests/10.0", {}, 4)
            self.assertEqual(result.status, 302)
            factory.assert_called_once_with("mcr.microsoft.com", timeout=10)
            response.read.assert_called_once_with(5)
            connection.request.assert_called_once()
            self.assertEqual(connection.request.call_args.args[:2],
                             ("GET", "/v2/dotnet/sdk/manifests/10.0"))
            self.assertNotIn("Authorization", connection.request.call_args.kwargs["headers"])
            connection.close.assert_called_once()
        response.read.return_value = b"12345"
        with (
            patch.object(drift.http.client, "HTTPSConnection", return_value=connection),
            self.assertRaisesRegex(drift.ObservationError, "response_too_large"),
        ):
            drift.https_get("https://mcr.microsoft.com/v2/dotnet/sdk/manifests/10.0", {}, 4)
        with patch.object(drift.http.client, "HTTPSConnection") as factory:
            with self.assertRaises(drift.ObservationError):
                drift.https_get("https://attacker.example/private", {}, 4)
            factory.assert_not_called()

    def test_process_deadline_and_invalid_results_fail_closed(self):
        pin = pin_for(index_response())
        valid = drift.observe(pin, Mock(return_value=index_response()))
        with patch.object(drift.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(valid).encode(),
        )) as run:
            self.assertEqual(drift.observe_bounded(pin), valid)
            self.assertEqual(run.call_args.kwargs.get("timeout"), 30)
        for failure in (
            subprocess.TimeoutExpired("private-command", 30),
            OSError("private-process-error"),
        ):
            with patch.object(drift.subprocess, "run", side_effect=failure):
                result = drift.observe_bounded(pin)
                self.assertIsNotNone(result["error"])
                self.assertNotIn("private", json.dumps(result))
        for payload in (
            b"private-invalid", b"{}", b'{"observedDigest":true}',
            json.dumps({**valid, "observedDigest": None}).encode(),
            json.dumps({**valid, "observedAt": "2026-09-09T12:00:00"}).encode(),
        ):
            with patch.object(drift.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=payload)):
                result = drift.observe_bounded(pin)
                self.assertIsNotNone(result["error"])
                self.assertIsNone(result["observedDigest"])
                self.assertNotIn("private", json.dumps(result))

    def test_cli_exit_codes_match_coverage_and_never_rewrite_pins(self):
        pin = pin_for(index_response())
        valid = drift.observe(pin, Mock(return_value=index_response()))
        paths = drift.tracked_dockerfiles(ROOT)
        before = {path: (ROOT / path).read_bytes() for path in paths}
        for status, exit_code in (("current", 0), ("changed", 1), ("unknown", 2)):
            report = {**drift.make_report([pin], lambda _pin: valid), "status": status}
            with patch.object(drift, "make_report", return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(drift.main(["--format", "json"]), exit_code)
            self.assertEqual(json.loads(output.getvalue())["status"], status)
        self.assertEqual(before, {path: (ROOT / path).read_bytes() for path in paths})

    def test_observation_deadline_really_terminates_an_unresponsive_worker(self):
        run = subprocess.run

        def sleeping_worker(_command, **kwargs):
            return run([sys.executable, "-c", "import time; time.sleep(2)"], **kwargs)

        started = time.monotonic()
        with (
            patch.object(drift, "OBSERVATION_TIMEOUT", 0.2),
            patch.object(drift.subprocess, "run", side_effect=sleeping_worker),
        ):
            result = drift.observe_bounded(pin_for(index_response()))
        self.assertEqual(result["error"], "observation_deadline_exceeded")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNone(result["observedDigest"])

    def test_source_failures_and_oversized_reports_cannot_look_complete(self):
        with (
            patch.object(drift, "collect_pins", side_effect=drift.BaseSourceError("invalid_base_pin")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(drift.main(["--format", "json"]), 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "invalid_base_pin")
        report = {"status": "current", "bases": ["x" * drift.MAX_REPORT_BYTES]}
        with (
            patch.object(drift, "make_report", return_value=report),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(drift.main(["--format", "json"]), 2)
        self.assertLess(len(output.getvalue().encode()), drift.MAX_REPORT_BYTES)
        self.assertEqual(json.loads(output.getvalue())["error"], "report_too_large")


if __name__ == "__main__":
    unittest.main()
