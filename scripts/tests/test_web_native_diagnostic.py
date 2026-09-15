"""Offline controls. Synthetic native-loader fixtures are NOT Windows SWC evidence."""
from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zlib
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "diagnose-web-native-lock.py"
PROBE = ROOT / "scripts" / "_web_native_probe.cjs"
SPEC = importlib.util.spec_from_file_location("web_native_diagnostic", SCRIPT)
assert SPEC and SPEC.loader
diag = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = diag
SPEC.loader.exec_module(diag)


def sri(data):
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def lock_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def fixture():
    native = {
        "name": diag.NATIVE, "version": diag.VERSION,
        "dist": {"tarball": diag.tarball_url(diag.NATIVE), "integrity": sri(b"synthetic native tarball")},
        "cpu": ["x64"], "os": ["win32"], "engines": {"node": ">= 10"}, "license": "MIT",
        "binarySha256": hashlib.sha256(b"synthetic native binary").hexdigest(),
    }
    next_metadata = {
        "name": "next", "version": diag.VERSION,
        "dist": {"tarball": diag.tarball_url("next"), "integrity": sri(b"synthetic next tarball")},
        "optionalDependencies": {diag.NATIVE: diag.VERSION, "@next/swc-darwin-arm64": diag.VERSION},
    }
    lock = {
        "lockfileVersion": 3, "name": "synthetic",
        "packages": {
            "": {"dependencies": {"next": diag.VERSION}},
            "node_modules/next": {
                "version": diag.VERSION, "resolved": next_metadata["dist"]["tarball"],
                "integrity": next_metadata["dist"]["integrity"],
                "optionalDependencies": next_metadata["optionalDependencies"],
            },
            "node_modules/@next/swc-darwin-arm64": {"version": diag.VERSION, "optional": True},
        },
    }
    metadata = {
        "node": diag.NODE_VERSION, "npm": diag.NPM_VERSION, "nodeMetadataSource": diag.NODE_INDEX,
        "packages": {"next": next_metadata, diag.NATIVE: native},
        "operations": {
            name: {"status": "succeeded", "url": url, "bytes": 10, "sha512": sri(b"synthetic public response")[7:]}
            for name, url in (
                ("node_release", diag.NODE_INDEX), ("next_metadata", diag.metadata_url("next")),
                ("next_tarball", diag.tarball_url("next")), ("native_metadata", diag.metadata_url(diag.NATIVE)),
                ("native_tarball", diag.tarball_url(diag.NATIVE)),
            )
        },
    }
    return lock, metadata


def add_record(lock, native):
    candidate = copy.deepcopy(lock)
    candidate["packages"][diag.NATIVE_KEY] = {
        "version": diag.VERSION, "resolved": native["dist"]["tarball"],
        "integrity": native["dist"]["integrity"], "optional": True,
        **{key: native[key] for key in ("cpu", "os", "license", "engines")},
    }
    return candidate


def archive_bytes(files, compressed=False):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz" if compressed else "w:") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return result.getvalue()


def native_result(native):
    return diag.encode({
        "native": True, "node": diag.NODE_VERSION, "platform": "win32", "arch": "x64",
        "name": diag.NATIVE, "version": diag.VERSION,
        "binarySha256": native["binarySha256"], "result": 42,
    })


def worker_report_category(output):
    if not output:
        return "worker_report_missing"
    if len(output) > diag.REPORT_LIMIT:
        return "worker_report_oversized"
    try:
        report = diag.object_json(output)
    except diag.DiagnosticError:
        return "worker_report_invalid"
    reason = report.get("error")
    if reason is not None:
        allowed = {
            "only_pr477_windows_actions", "unexpected_pull_request", "invalid_source_sha",
            "invalid_workflow_sha", "invalid_workflow_identity", "invalid_worker_directory",
            "invalid_package_archive", "public_worker_io_failure", "public_report_limit",
        }
        return reason if isinstance(reason, str) and reason in allowed else "worker_error_unrecognized"
    return "worker_operations_present" if isinstance(report.get("operations"), dict) else "worker_operations_missing"


class WebNativeDiagnosticTests(unittest.TestCase):
    def test_original_hashes_bind_git_blobs_not_windows_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            env = diag.child_env(dict(os.environ), Path(directory) / "home")
            blobs = {
                name: subprocess.check_output(
                    ["git", "show", f"{diag.ORIGINAL}:app/web/{name}"], cwd=ROOT, env=env,
                ) for name in diag.ORIGINAL_HASHES
            }
            for name, expected in diag.ORIGINAL_HASHES.items():
                self.assertEqual(hashlib.sha256(blobs[name]).hexdigest(), expected)
        lock = json.loads(blobs["package-lock.json"])
        declared = lock["packages"]["node_modules/next"]["optionalDependencies"]
        binaries = {name for name in declared if name.startswith("@next/swc-")}
        self.assertEqual({name for name in binaries if f"node_modules/{name}" not in lock["packages"]},
                         {diag.NATIVE})

    def test_identity_requires_exact_pr_repository_event_runner_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            payload = {
                "number": 477, "pull_request": {
                    "number": 477, "head": {"sha": "a" * 40, "repo": {"full_name": diag.REPOSITORY}},
                },
            }
            event.write_bytes(diag.encode(payload))
            env = {
                "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": diag.REPOSITORY,
                "GITHUB_SERVER_URL": "https://github.com", "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REF": "refs/pull/477/merge", "RUNNER_OS": "Windows", "RUNNER_ARCH": "X64",
                "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": "b" * 40, "GITHUB_WORKFLOW_SHA": "b" * 40,
                "GITHUB_WORKFLOW_REF": f"{diag.REPOSITORY}/.github/workflows/app-ci.yml@refs/pull/477/merge",
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            }
            self.assertEqual(diag.identity(env)["sourceCommit"], "a" * 40)
            for key, value in (
                ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_EVENT_NAME", "pull_request_target"),
                ("GITHUB_REF", "refs/heads/main"), ("RUNNER_OS", "Linux"), ("RUNNER_ARCH", "ARM64"),
                ("GITHUB_WORKFLOW_REF", "wrong"), ("GITHUB_RUN_ATTEMPT", "0"),
            ):
                with self.subTest(key=key), self.assertRaises(diag.DiagnosticError):
                    diag.identity({**env, key: value})
            for replacement in ({"number": 478}, {"head": {"sha": "a" * 40, "repo": {"full_name": "fork/repo"}}}):
                mutated = copy.deepcopy(payload)
                mutated["pull_request"].update(replacement)
                event.write_bytes(diag.encode(mutated))
                with self.assertRaises(diag.DiagnosticError):
                    diag.identity(env)
            event.write_bytes(diag.encode(payload))
            self.assertEqual(diag.identity(env)["pullRequest"], 477)

    def test_child_environment_excludes_credentials_loaders_and_registry_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            env = diag.child_env({
                "Path": "toolchain", "SystemRoot": "system",
                "GITHUB_TOKEN": "private-marker", "ACTIONS_RUNTIME_TOKEN": "private-marker",
                "AZURE_CLIENT_SECRET": "private-marker", "NODE_OPTIONS": "--require attacker.cjs",
                "NODE_PATH": "outside", "NPM_CONFIG_REGISTRY": "https://private.invalid",
                "HTTP_PROXY": "private-marker", "NPM_TOKEN": "private-marker",
            }, Path(directory) / "home")
            self.assertEqual(env["PATH"], "toolchain")
            self.assertEqual(env["NPM_CONFIG_REGISTRY"], diag.REGISTRY)
            self.assertEqual(env["NPM_CONFIG_IGNORE_SCRIPTS"], "true")
            self.assertEqual(env["NPM_CONFIG_STRICT_SSL"], "true")
            self.assertEqual(env["NEXT_TELEMETRY_DISABLED"], "1")
            self.assertNotIn("private-marker", json.dumps(env))
            self.assertNotIn("NODE_OPTIONS", env)
            self.assertNotIn("NODE_PATH", env)
            self.assertEqual(Path(env["NPM_CONFIG_USERCONFIG"]).read_bytes(), b"")

    def test_strict_json_rejects_duplicates_and_non_json_numbers(self):
        self.assertEqual(diag.object_json(b'{"value":1}'), {"value": 1})
        for body in (b'{"value":1,"value":2}', b'{"value":NaN}', b'{"value":Infinity}', b'[]', b'{'):
            with self.subTest(body=body), self.assertRaises(diag.DiagnosticError):
                diag.object_json(body)

    def test_public_metadata_must_match_exact_published_package_and_existing_next_lock(self):
        lock, metadata = fixture()
        for name, value in metadata["packages"].items():
            self.assertEqual(diag.verify_metadata(name, value, lock)["name"], name)
            mutations = [
                {"name": "different"}, {"version": "16.4.0"},
                {"dist": {**value["dist"], "tarball": "https://private.invalid/package.tgz"}},
                {"dist": {**value["dist"], "integrity": "sha512-AAAA"}},
            ]
            if name == diag.NATIVE:
                mutations.extend(({"os": ["linux"]}, {"cpu": ["arm64"]}))
            else:
                mutations.append({"optionalDependencies": {}})
                mutations.append({"dist": {**value["dist"], "integrity": sri(b"another archive")}})
            for mutation in mutations:
                with self.subTest(name=name, mutation=mutation), self.assertRaises(diag.DiagnosticError):
                    diag.verify_metadata(name, {**value, **mutation}, lock)
            self.assertEqual(diag.verify_metadata(name, value, lock)["version"], diag.VERSION)

    def test_candidate_accepts_only_one_verified_record_and_unchanged_manifest(self):
        lock, metadata = fixture()
        native = metadata["packages"][diag.NATIVE]
        candidate = add_record(lock, native)
        manifest = b'{"dependencies":{"next":"16.3.5"}}\n'

        def verify(value=candidate, candidate_manifest=manifest):
            diag.verify_candidate(manifest, lock_bytes(lock), candidate_manifest, lock_bytes(value), native)

        verify()
        with self.assertRaises(diag.DiagnosticError):
            verify(lock)
        with self.assertRaises(diag.DiagnosticError):
            verify(candidate_manifest=manifest + b" ")
        mutations = []
        changed = copy.deepcopy(candidate)
        changed["packages"]["node_modules/next"]["version"] = "16.4.0"
        mutations.append(changed)
        changed = copy.deepcopy(candidate)
        del changed["packages"]["node_modules/@next/swc-darwin-arm64"]
        mutations.append(changed)
        changed = copy.deepcopy(candidate)
        changed["packages"]["node_modules/unrelated"] = {"version": "1.0.0"}
        mutations.append(changed)
        changed = copy.deepcopy(candidate)
        changed["packages"][""]["dependencies"][diag.NATIVE] = diag.VERSION
        mutations.append(changed)
        for field, value in (
            ("optional", 1), ("integrity", sri(b"invented")), ("version", "16.4.0"),
            ("resolved", "https://mirror.invalid/native.tgz"), ("cpu", ["arm64"]),
        ):
            changed = copy.deepcopy(candidate)
            changed["packages"][diag.NATIVE_KEY][field] = value
            mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(diag.DiagnosticError):
                verify(changed)
        with self.assertRaisesRegex(diag.DiagnosticError, "candidate_format_changed"):
            diag.verify_candidate(manifest, lock_bytes(lock), manifest, diag.encode(candidate), native)
        reordered = copy.deepcopy(candidate)
        reordered["packages"]["node_modules/next"] = dict(reversed(
            list(reordered["packages"]["node_modules/next"].items()),
        ))
        with self.assertRaisesRegex(diag.DiagnosticError, "existing_lock_text_changed"):
            verify(reordered)
        verify()

    def test_native_evidence_requires_real_shape_and_matching_binary_subject(self):
        _, metadata = fixture()
        native = metadata["packages"][diag.NATIVE]
        good = native_result(native)
        self.assertTrue(diag.verify_native(good, native)["native"])
        for change in (
            {"native": False}, {"native": 1}, {"result": 42.0}, {"platform": "linux"},
            {"arch": "arm64"}, {"binarySha256": "0" * 64}, {"version": "16.3.4"},
        ):
            with self.subTest(change=change), self.assertRaises(diag.DiagnosticError):
                diag.verify_native(diag.encode({**json.loads(good), **change}), native)
        for data in (b"", b"{}", b'{"native":true}'):
            with self.assertRaises(diag.DiagnosticError):
                diag.verify_native(data, native)
        self.assertEqual(diag.verify_native(good, native)["result"], 42)

    def test_public_operation_report_refuses_private_urls_errors_and_missing_coverage(self):
        _, metadata = fixture()
        operations = metadata["operations"]
        diag.verify_operations(operations)
        for change in (
            {"url": "https://private.invalid"}, {"reason": "private credential value"},
            {"bytes": True}, {"sha512": ""}, {"unexpected": "private-marker"},
        ):
            changed = copy.deepcopy(operations)
            changed["native_tarball"].update(change)
            if "reason" in change:
                changed["native_tarball"]["status"] = "failed"
            with self.subTest(change=change), self.assertRaises(diag.DiagnosticError):
                diag.verify_operations(changed)
        with self.assertRaises(diag.DiagnosticError):
            diag.verify_operations({})
        diag.verify_operations(operations)

    def test_source_archive_is_real_files_with_no_install_tree_config_or_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diag.extract_source(archive_bytes({"app/web/package.json": b"{}", "app/web/.env.example": b""}), root / "good")
            self.assertEqual((root / "good" / "app" / "web" / "package.json").read_bytes(), b"{}")
            for index, name in enumerate((
                "../escape", "app/web/../../escape", "app\\web\\escape",
                "app/web/node_modules/module.js", "app/web/.npmrc", "app/web/.env.local", "scripts/unrelated.py",
            )):
                with self.subTest(name=name), self.assertRaises(diag.DiagnosticError):
                    diag.extract_source(archive_bytes({name: b"bad"}), root / str(index))
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:") as archive:
                link = tarfile.TarInfo("app/web/link")
                link.type = tarfile.SYMTYPE
                link.linkname = ".."
                archive.addfile(link)
            with self.assertRaises(diag.DiagnosticError):
                diag.extract_source(data.getvalue(), root / "link")

    def test_download_has_public_allowlist_identity_encoding_size_deadline_and_no_redirects(self):
        class Response:
            status = 200

            def __init__(self, body=b"1234567890", headers=None):
                self.data = io.BytesIO(body)
                self.headers = Message()
                for key, value in (headers or {}).items():
                    self.headers[key] = value

            def read(self, size):
                return self.data.read(size)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = Response()
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.object(diag.http.client, "HTTPSConnection", return_value=connection) as factory:
                digest = diag.download(diag.metadata_url("next"), root / "good", 10)
                self.assertEqual(digest, hashlib.sha512(b"1234567890").digest())
                self.assertEqual((root / "good").read_bytes(), b"1234567890")
                self.assertEqual(factory.call_args.kwargs["timeout"], 10)
                self.assertNotIn("Authorization", connection.request.call_args.kwargs["headers"])
                self.assertNotIn("Cookie", connection.request.call_args.kwargs["headers"])
                connection.reset_mock()
                for url in (
                    "http://registry.npmjs.org/next/16.3.5",
                    "https://registry.npmjs.org.evil.invalid/next/16.3.5",
                    "https://registry.npmjs.org/next/16.3.5?secret=value",
                    "https://user:secret@registry.npmjs.org/next/16.3.5",
                ):
                    with self.assertRaises(diag.DiagnosticError):
                        diag.download(url, root / "bad-url", 10)
                connection.request.assert_not_called()
                for index, bad_response in enumerate((
                    Response(b"12345678901"),
                    Response(headers={"Content-Encoding": "gzip"}),
                    Response(headers={"Content-Length": "11"}),
                    Response(headers={"Content-Length": "9"}),
                )):
                    connection.getresponse.return_value = bad_response
                    with self.assertRaises(diag.DiagnosticError):
                        diag.download(diag.metadata_url("next"), root / f"bad-{index}", 10)
                redirected = Response()
                redirected.status = 302
                connection.getresponse.return_value = redirected
                connection.reset_mock()
                with self.assertRaises(diag.DiagnosticError):
                    diag.download(diag.metadata_url("next"), root / "redirect", 10)
                self.assertEqual(connection.request.call_count, 1)
                connection.getresponse.return_value = Response()
                with patch.object(diag.time, "monotonic", side_effect=[0, 31]):
                    with self.assertRaisesRegex(diag.DiagnosticError, "download_deadline"):
                        diag.download(diag.metadata_url("next"), root / "deadline", 10)

    def test_public_collection_hashes_actual_archive_bytes_and_checks_node_release_npm(self):
        lock, metadata = fixture()
        next_metadata = metadata["packages"]["next"]
        native = metadata["packages"][diag.NATIVE]
        node_release = [{
            "version": f"v{diag.NODE_VERSION}", "npm": diag.NPM_VERSION, "files": ["win-x64-zip"],
        }]
        native_binary = b"synthetic native archive member"
        next_tgz = archive_bytes({"package/package.json": diag.encode({
            "name": "next", "version": diag.VERSION, "optionalDependencies": next_metadata["optionalDependencies"],
        })}, True)
        native_tgz = archive_bytes({
            "package/package.json": diag.encode({
                "name": diag.NATIVE, "version": diag.VERSION, "cpu": ["x64"], "os": ["win32"],
                "main": "native.node",
            }),
            "package/native.node": native_binary,
        }, True)
        next_metadata["dist"]["integrity"] = sri(next_tgz)
        lock["packages"]["node_modules/next"]["integrity"] = sri(next_tgz)
        native["dist"]["integrity"] = sri(native_tgz)
        responses = {
            diag.NODE_INDEX: diag.encode(node_release),
            diag.metadata_url("next"): diag.encode(next_metadata),
            diag.metadata_url(diag.NATIVE): diag.encode(native),
            diag.tarball_url("next"): next_tgz, diag.tarball_url(diag.NATIVE): native_tgz,
        }
        calls = []

        def download(url, destination, limit):
            calls.append(url)
            data = responses[url]
            self.assertLessEqual(len(data), limit)
            destination.write_bytes(data)
            return hashlib.sha512(data).digest()

        with tempfile.TemporaryDirectory() as directory, patch.object(diag, "download", side_effect=download):
            root = Path(directory)
            result = diag.collect_public(root, lock)
            self.assertEqual(len(calls), 5)
            self.assertEqual(result["packages"][diag.NATIVE]["binarySha256"],
                             hashlib.sha256(native_binary).hexdigest())
            responses[diag.NODE_INDEX] = diag.encode([{**node_release[0], "npm": "99.0.0"}])
            partial = {}
            with self.assertRaisesRegex(diag.DiagnosticError, "node_bundled_npm_mismatch"):
                diag.collect_public(root, lock, partial)
            diag.verify_operations(partial["operations"])
            self.assertEqual(partial["operations"]["node_release"]["status"], "failed")
            self.assertEqual(partial["operations"]["next_metadata"]["status"], "not_run")
            responses[diag.NODE_INDEX] = diag.encode(node_release)
            responses[diag.tarball_url(diag.NATIVE)] = archive_bytes({
                "package/package.json": diag.encode({
                    "name": diag.NATIVE, "version": diag.VERSION, "cpu": ["x64"], "os": ["win32"],
                    "main": "native.node",
                }),
                "package/native.node": native_binary + b"tampered",
            }, True)
            with self.assertRaisesRegex(diag.DiagnosticError, "tarball_integrity_mismatch"):
                diag.collect_public(root, lock)

    def test_public_transport_failure_retains_http_status_and_unrun_operations(self):
        lock, _ = fixture()
        partial = {}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            diag, "download", side_effect=diag.DiagnosticError("public_http_status", 403),
        ):
            with self.assertRaises(diag.DiagnosticError):
                diag.collect_public(Path(directory), lock, partial)
        diag.verify_operations(partial["operations"])
        self.assertEqual(partial["operations"]["node_release"]["httpStatus"], 403)
        self.assertTrue(all(partial["operations"][name]["status"] == "not_run"
                            for name in diag.PUBLIC_OPERATIONS[1:]))

    def test_collect_worker_preserves_completed_operations_for_matching_sri_invalid_deflate(self):
        lock, metadata = fixture()
        next_metadata = metadata["packages"]["next"]
        native_metadata = metadata["packages"][diag.NATIVE]
        next_tgz = archive_bytes({"package/package.json": diag.encode({
            "name": "next", "version": diag.VERSION, "optionalDependencies": next_metadata["optionalDependencies"],
        })}, True)
        native_binary = b"synthetic native archive member"
        native_tgz = archive_bytes({
            "package/package.json": diag.encode({
                "name": diag.NATIVE, "version": diag.VERSION, "cpu": ["x64"], "os": ["win32"],
                "main": "native.node",
            }),
            "package/native.node": native_binary,
        }, True)
        # Valid gzip framing, but reserved DEFLATE block type 3.
        malformed_tgz = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07" + b"\x00" * 8
        with self.assertRaises(zlib.error):
            gzip.decompress(malformed_tgz)
        next_metadata["dist"]["integrity"] = sri(next_tgz)
        lock["packages"]["node_modules/next"]["integrity"] = sri(next_tgz)
        worker = """
import base64, hashlib, importlib.util, json, sys
from pathlib import Path
script, response_path, public, lock_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("deflate_worker", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
responses = json.loads(Path(response_path).read_bytes())
def download(url, destination, limit):
    body = base64.b64decode(responses[url], validate=True)
    assert len(body) <= limit
    destination.write_bytes(body)
    return hashlib.sha512(body).digest()
module.download = download
sys.argv = [script, "--collect", public, "--lock", lock_path]
raise SystemExit(module.main())
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            public = root / "pr477-native-deflate-control" / "public"
            public.mkdir(parents=True)
            lock_path = public.parent / "source" / "app" / "web" / "package-lock.json"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_bytes(lock_bytes(lock))
            event_path = root / "event.json"
            event_path.write_bytes(diag.encode({
                "number": 477, "pull_request": {
                    "number": 477, "head": {"sha": "a" * 40, "repo": {"full_name": diag.REPOSITORY}},
                },
            }))
            env = {
                **diag.child_env(dict(os.environ), root / "home"),
                "RUNNER_TEMP": str(root), "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": diag.REPOSITORY, "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/477/merge",
                "GITHUB_EVENT_PATH": str(event_path), "RUNNER_OS": "Windows", "RUNNER_ARCH": "X64",
                "GITHUB_SHA": "b" * 40, "GITHUB_WORKFLOW_SHA": "b" * 40,
                "GITHUB_WORKFLOW_REF": f"{diag.REPOSITORY}/.github/workflows/app-ci.yml@refs/pull/477/merge",
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            }
            responses_path = root / "responses.json"
            for index, tarball in enumerate((native_tgz, malformed_tgz, native_tgz)):
                with self.subTest(control=index):
                    native_metadata["dist"]["integrity"] = sri(tarball)
                    responses = {
                        diag.NODE_INDEX: diag.encode([{
                            "version": f"v{diag.NODE_VERSION}", "npm": diag.NPM_VERSION,
                            "files": ["win-x64-zip"],
                        }]),
                        diag.metadata_url("next"): diag.encode(next_metadata),
                        diag.tarball_url("next"): next_tgz,
                        diag.metadata_url(diag.NATIVE): diag.encode(native_metadata),
                        diag.tarball_url(diag.NATIVE): tarball,
                    }
                    responses_path.write_bytes(diag.encode({
                        url: base64.b64encode(body).decode() for url, body in responses.items()
                    }))
                    result = subprocess.run(
                        [sys.executable, "-c", worker, str(SCRIPT), str(responses_path),
                         str(public), str(lock_path)],
                        cwd=root, env=env, stdin=subprocess.DEVNULL,
                        capture_output=True, timeout=10, check=False,
                    )
                    category = worker_report_category(result.stdout)
                    self.assertEqual(result.returncode, 2 if index == 1 else 0, category)
                    self.assertFalse(bool(result.stderr), category)
                    self.assertGreater(len(result.stdout), 0)
                    self.assertLessEqual(len(result.stdout), diag.REPORT_LIMIT)
                    report = diag.object_json(result.stdout)
                    self.assertTrue(isinstance(report.get("operations"), dict), category)
                    operations = report["operations"]
                    diag.verify_operations(operations)
                    self.assertEqual({name: row["status"] for name, row in operations.items()}, {
                        name: "failed" if index == 1 and name == "native_tarball" else "succeeded"
                        for name in diag.PUBLIC_OPERATIONS
                    })
                    self.assertEqual(operations["native_tarball"]["sha512"], sri(tarball)[7:])
                    self.assertEqual(operations["native_tarball"]["bytes"], len(tarball))
                    self.assertIn("next", report["packages"])
                    if index == 1:
                        self.assertEqual(report["error"], "invalid_package_archive")
                        self.assertEqual(operations["native_tarball"]["reason"], "invalid_package_archive")
                        self.assertIsNone(operations["native_tarball"]["httpStatus"])
                        self.assertNotIn(diag.NATIVE, report["packages"])
                    else:
                        self.assertNotIn("error", report)
                        self.assertEqual(report["packages"][diag.NATIVE]["binarySha256"],
                                         hashlib.sha256(native_binary).hexdigest())

    def test_collect_worker_ignores_inherited_hosted_identity_metadata(self):
        synthetic = {
            "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "hosted-fixture/example",
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/999/merge",
            "GITHUB_SHA": "c" * 40, "GITHUB_WORKFLOW_SHA": "d" * 40,
            "GITHUB_WORKFLOW_REF": "hosted-fixture/example/.github/workflows/test.yml@refs/pull/999/merge",
            "GITHUB_RUN_ID": "999999", "GITHUB_RUN_ATTEMPT": "3",
            "GITHUB_EVENT_PATH": r"C:\synthetic-hosted-fixture\event.json",
            "GITHUB_WORKSPACE": r"D:\a\synthetic-hosted-fixture\synthetic-hosted-fixture",
            "RUNNER_OS": "Windows", "RUNNER_ARCH": "X64",
            "RUNNER_TEMP": r"D:\a\synthetic-hosted-fixture\_temp",
        }
        with patch.object(os, "environ", {**os.environ, **synthetic}):
            self.test_collect_worker_preserves_completed_operations_for_matching_sri_invalid_deflate()

    def test_collect_worker_canonicalizes_fixture_temp_paths(self):
        with tempfile.TemporaryDirectory(prefix="pr477 hosted temp ") as directory:
            root = Path(directory).resolve()
            intermediate = root / "indirect"
            intermediate.mkdir()
            aliases = [intermediate / ".."]
            if sys.platform == "win32":
                from ctypes import wintypes

                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
                api.GetShortPathNameW.restype = wintypes.DWORD
                buffer = ctypes.create_unicode_buffer(32768)
                length = api.GetShortPathNameW(str(root), buffer, len(buffer))
                self.assertTrue(0 < length < len(buffer))
                short = Path(buffer.value)
                if short != root:
                    aliases.append(short)
            for alias in aliases:
                with self.subTest(alias_kind="short" if alias.name != ".." else "parent"):
                    self.assertNotEqual(alias, root)
                    self.assertEqual(alias.resolve(), root)
                    with patch.object(tempfile, "tempdir", str(alias)):
                        self.test_collect_worker_preserves_completed_operations_for_matching_sri_invalid_deflate()

    def test_worker_failure_assertions_expose_only_bounded_allowlisted_categories(self):
        self.assertEqual(worker_report_category(diag.encode({"error": "invalid_worker_directory"})),
                         "invalid_worker_directory")
        for output, expected in (
            (b"", "worker_report_missing"),
            (b"{" + b"x" * diag.REPORT_LIMIT, "worker_report_oversized"),
            (b"private-marker", "worker_report_invalid"),
            (diag.encode({"error": "private-marker"}), "worker_error_unrecognized"),
            (diag.encode({"error": {"private-marker": True}}), "worker_error_unrecognized"),
            (diag.encode({"unexpected": "private-marker"}), "worker_operations_missing"),
            (diag.encode({"operations": {}}), "worker_operations_present"),
        ):
            category = worker_report_category(output)
            self.assertEqual(category, expected)
            self.assertNotIn("private-marker", category)

    def test_tarball_rejects_wrong_platform_main_or_package_identity(self):
        _, metadata = fixture()
        native = metadata["packages"][diag.NATIVE]
        manifest = {
            "name": diag.NATIVE, "version": diag.VERSION, "cpu": ["x64"], "os": ["win32"], "main": "native.node",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.tgz"

            def write(value):
                path.write_bytes(archive_bytes({
                    "package/package.json": diag.encode(value), "package/native.node": b"synthetic",
                }, True))

            write(manifest)
            self.assertIn("binarySha256", diag.inspect_tarball(path, native))
            for change in (
                {"name": "wrong"}, {"version": "16.4.0"}, {"os": ["linux"]}, {"cpu": ["arm64"]},
                {"main": "index.js"}, {"scripts": {"postinstall": "unapproved"}},
            ):
                write({**manifest, **change})
                with self.subTest(change=change), self.assertRaises(diag.DiagnosticError):
                    diag.inspect_tarball(path, native)
            write(manifest)
            self.assertIn("binarySha256", diag.inspect_tarball(path, native))

    def test_process_exit_output_limit_timeout_and_redaction_are_not_success_shaped(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            env = diag.child_env(dict(os.environ), cwd / "home")
            success = diag.run_process([sys.executable, "-c", "print('ok')"], cwd, env, 5)
            self.assertEqual((success.status, success.exit_code), ("succeeded", 0))
            failed = diag.run_process([
                sys.executable, "-c",
                "print('npm error code EUSAGE\\nnpm error code E_PRIVATE_MARKER\\nprivate-marker'); raise SystemExit(7)",
            ], cwd, env, 5)
            self.assertEqual((failed.status, failed.exit_code), ("failed", 7))
            self.assertEqual(failed.summary()["npmErrorCodes"], ["EUSAGE"])
            self.assertNotIn("private-marker", json.dumps(failed.summary()))
            self.assertNotIn("E_PRIVATE_MARKER", json.dumps(failed.summary()))
            limited = diag.run_process([sys.executable, "-c", "print('x' * 100000)"], cwd, env, 5, 16)
            self.assertEqual((limited.status, limited.reason), ("failed", "output_limit"))
            self.assertLessEqual(len(limited.output), 16)
            timed_out = diag.run_process([sys.executable, "-c", "import time; time.sleep(10)"], cwd, env, 0.2)
            self.assertEqual((timed_out.status, timed_out.reason), ("failed", "command_timeout"))
            self.assertIsNotNone(timed_out.exit_code)
            unrun = diag.run_process([sys.executable, "-c", "raise SystemExit(99)"], cwd, env, 0)
            self.assertEqual((unrun.status, unrun.exit_code), ("not_run", None))

    def test_all_declared_stages_and_zero_exit_validation_failures_survive_report(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = diag.Diagnostic(Path(directory), {})
            self.assertEqual(set(diagnostic.report["stages"]), set(diag.STAGES))
            for value in diagnostic.report["stages"].values():
                self.assertEqual((value["status"], value["exitCode"]), ("not_run", None))
            with patch.object(diag, "run_process", return_value=diag.CommandResult("succeeded", 0, b"{}")):
                diagnostic.command("candidate_native", ["node"], Path(directory), {}, 1)
            diagnostic.failure("native_evidence_mismatch")
            diagnostic.finish()
            diagnostic.save()
            report = json.loads((Path(directory) / "report.json").read_bytes())
            self.assertEqual(report["commands"][0]["exitCode"], 0)
            self.assertEqual(report["stages"]["candidate_native"]["status"], "failed")
            self.assertFalse(report["candidateValidated"])
            self.assertFalse(report["adoptionAuthorized"])
            diagnostic.report.update(candidateValidated=True, originalValidated=True)
            diagnostic.failure("temporary_cleanup_failed")
            self.assertFalse(diagnostic.report["candidateValidated"])
            self.assertFalse(diagnostic.report["originalValidated"])

    def test_cleanup_unknown_and_command_limit_preserve_missing_exit_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = diag.Diagnostic(Path(directory), {})
            with patch.object(diag, "run_process", side_effect=diag.DiagnosticError("command_cleanup_unknown")):
                with self.assertRaises(diag.DiagnosticError):
                    diagnostic.command("candidate_install", ["node"], Path(directory), {}, 1)
            self.assertEqual(diagnostic.report["commands"][0]["exitCode"], None)
            self.assertEqual(diagnostic.report["commands"][0]["reason"], "command_cleanup_unknown")
            diagnostic.report["commands"] *= 48
            with patch.object(diag, "run_process") as runner, self.assertRaises(diag.DiagnosticError):
                diagnostic.command("candidate_build", ["node"], Path(directory), {}, 1)
            runner.assert_not_called()

    def test_main_finishes_immutability_before_temporary_files_are_removed(self):
        def fake_run(instance, work):
            path = work / "lock"
            path.write_bytes(b"original")
            instance.immutable = {path: b"original"}
            instance.control_env = {}
            for stage in diag.STAGES:
                instance.success(stage)
            instance.report["stages"]["original_guard"].update(status="failed", exitCode=1)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pr477-native-evidence"
            with (
                patch.object(diag.os, "environ", {**os.environ, "RUNNER_TEMP": directory}),
                patch.object(sys, "argv", [str(SCRIPT), "--output", str(output)]),
                patch.object(diag.Diagnostic, "run", fake_run),
                patch.object(diag, "run_process", return_value=diag.CommandResult("succeeded", 0)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = diag.main()
            report = json.loads((output / "report.json").read_bytes())
            self.assertEqual(code, 1)
            self.assertTrue(report["candidateValidated"])
            self.assertFalse(report["originalValidated"])
            self.assertEqual(report["status"], "candidate_verified_original_failed")
            self.assertEqual(report["stages"]["immutability"]["status"], "succeeded")
            self.assertEqual(set(path.name for path in output.iterdir()), {"report.json"})

    def test_off_actions_entry_refuses_before_any_process_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pr477-native-evidence"
            with (
                patch.object(diag.os, "environ", {"RUNNER_TEMP": directory}),
                patch.object(sys, "argv", [str(SCRIPT), "--output", str(output)]),
                patch.object(diag, "run_process") as command,
                patch.object(diag.http.client, "HTTPSConnection") as network,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = diag.main()
            self.assertEqual(code, 1)
            command.assert_not_called()
            network.assert_not_called()
            report = json.loads((output / "report.json").read_bytes())
            self.assertEqual(report["stages"]["source"]["status"], "failed")
            self.assertEqual(report["stages"]["public_packages"]["status"], "not_run")
            self.assertFalse(report["candidateValidated"])


class ArchiveStreamLimitTests(unittest.TestCase):
    def test_complete_decompressed_stream_is_bounded_before_tar_parsing(self):
        _, metadata = fixture()
        native = metadata["packages"][diag.NATIVE]
        manifest = diag.encode({
            "name": diag.NATIVE, "version": diag.VERSION, "cpu": ["x64"], "os": ["win32"],
            "main": "native.node",
        })
        for kind in ("pax", "gnu", "member", "padding"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                raw = io.BytesIO()
                options = {"format": tarfile.GNU_FORMAT} if kind == "gnu" else {"format": tarfile.PAX_FORMAT}
                if kind == "pax":
                    options["pax_headers"] = {"comment": "x" * 8192}
                with tarfile.open(fileobj=raw, mode="w:", **options) as archive:
                    members = {"package/package.json": manifest, "package/native.node": b"synthetic"}
                    if kind == "gnu":
                        members["package/" + "x" * 8192] = b"small"
                    if kind == "member":
                        members["package/large.txt"] = b"x" * 8192
                    for name, body in members.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(body)
                        archive.addfile(member, io.BytesIO(body))
                body = raw.getvalue() + (b"\0" * 8192 if kind == "padding" else b"")
                path = Path(directory) / "package.tgz"
                path.write_bytes(gzip.compress(body))
                with patch.object(diag, "UNPACKED_LIMIT", len(body)):
                    self.assertIn("binarySha256", diag.inspect_tarball(path, native))
                for limit in (8192, len(body) - 1):
                    with (
                        patch.object(diag, "UNPACKED_LIMIT", limit),
                        patch.object(diag.tarfile, "open", wraps=tarfile.open) as parser,
                    ):
                        with self.assertRaisesRegex(diag.DiagnosticError, "package_archive_limit"):
                            diag.inspect_tarball(path, native)
                        parser.assert_not_called()
                with patch.object(diag, "UNPACKED_LIMIT", len(body)):
                    self.assertIn("binarySha256", diag.inspect_tarball(path, native))

    def test_temporary_decompressed_bytes_are_closed_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compressed = root / "bounded.gz"
            compressed.write_bytes(gzip.compress(b"x" * 1024))
            streams = []
            real_temporary = tempfile.TemporaryFile

            def temporary(**kwargs):
                stream = real_temporary(**kwargs)
                streams.append(stream)
                return stream

            with patch.object(diag.tempfile, "TemporaryFile", side_effect=temporary):
                with patch.object(diag, "UNPACKED_LIMIT", 1024):
                    with diag.bounded_tar_stream(compressed) as stream:
                        self.assertEqual(stream.read(), b"x" * 1024)
                with patch.object(diag, "UNPACKED_LIMIT", 1023):
                    with self.assertRaisesRegex(diag.DiagnosticError, "package_archive_limit"):
                        with diag.bounded_tar_stream(compressed):
                            self.fail("Oversized bytes were exposed to the parser")
            self.assertEqual(len(streams), 2)
            self.assertTrue(all(stream.closed for stream in streams))
            self.assertEqual(list(root.iterdir()), [compressed])


class ProcessCleanupContractTests(unittest.TestCase):
    def test_exited_leader_still_triggers_tree_termination_and_pipe_close(self):
        for descendants in (False, True):
            pipe = Mock()
            pipe.read.return_value = b""
            process = Mock(stdout=pipe, returncode=0)
            process.poll.return_value = 0
            with (
                patch.object(diag, "launch_process", return_value=process),
                patch.object(diag.os, "set_blocking"),
                patch.object(diag, "active_descendants", return_value=descendants),
                patch.object(diag, "stop_tree") as terminate,
            ):
                result = diag.run_process(["synthetic"], Path("."), {}, 1)
            terminate.assert_called_once_with(process)
            pipe.close.assert_called_once()
            self.assertEqual(result.status, "failed" if descendants else "succeeded")
            self.assertEqual(result.exit_code, 0)

    def test_pipe_errors_and_tree_cleanup_errors_still_close_the_pipe(self):
        for setup_error, terminate_error in ((OSError("private-marker"), None), (None, diag.DiagnosticError("command_cleanup_unknown"))):
            pipe = Mock()
            pipe.read.return_value = b""
            process = Mock(stdout=pipe, returncode=0)
            process.poll.return_value = 0
            with (
                patch.object(diag, "launch_process", return_value=process),
                patch.object(diag.os, "set_blocking", side_effect=setup_error),
                patch.object(diag, "active_descendants", return_value=False),
                patch.object(diag, "stop_tree", side_effect=terminate_error) as terminate,
            ):
                with self.assertRaisesRegex(diag.DiagnosticError, "^command_cleanup_unknown$"):
                    diag.run_process(["synthetic"], Path("."), {}, 1)
            terminate.assert_called_once_with(process)
            pipe.close.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "Actual Windows descendant and pipe lifetime controls")
class WindowsProcessLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ctypes import wintypes

        cls.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        cls.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        cls.kernel.OpenProcess.restype = wintypes.HANDLE
        cls.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        cls.kernel.WaitForSingleObject.restype = wintypes.DWORD
        cls.kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        cls.kernel.TerminateProcess.restype = wintypes.BOOL
        cls.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        cls.kernel.CloseHandle.restype = wintypes.BOOL
        cls.kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        cls.kernel.IsProcessInJob.restype = wintypes.BOOL

    def test_job_membership_is_established_inside_the_native_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = diag.child_env(dict(os.environ), root / "home")
            api = diag._WindowsAPI()
            update = api.update_attribute
            create = api.create_process
            order = []

            def update_attribute(*args):
                value = update(*args)
                order.append(args[2])
                return value

            def create_process(*args):
                order.append("create")
                return create(*args)

            api.update_attribute = update_attribute
            api.create_process = create_process
            process = None
            try:
                with patch.object(diag, "_WindowsAPI", return_value=api):
                    process = diag._WindowsJobProcess(
                        [sys.executable, "-c", "import time; time.sleep(20)"], root, env,
                    )
                member = ctypes.c_int32()
                self.assertTrue(self.kernel.IsProcessInJob(process.handle, process.job, ctypes.byref(member)))
                self.assertEqual(member.value, 1, "The launched process is not in its exact owned job")
                self.assertLess(order.index(0x2000D), order.index("create"))
                self.assertEqual(process.active_processes(), 1)
            finally:
                if process is not None:
                    self.kernel.TerminateProcess(process.handle, 1)
                    self.kernel.WaitForSingleObject(process.handle, 5000)
                    process.close()

    def test_job_enrollment_failure_never_starts_the_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "started"
            env = diag.child_env(dict(os.environ), root / "home")
            api = diag._WindowsAPI()
            update = api.update_attribute
            api.update_attribute = lambda *args: False if args[2] == 0x2000D else update(*args)
            api.create_process = Mock(wraps=api.create_process)
            command = [
                sys.executable, "-c",
                "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker),
            ]
            with patch.object(diag, "_WindowsAPI", return_value=api):
                with self.assertRaisesRegex(diag.DiagnosticError, "command_ownership_failed"):
                    diag.run_process(command, root, env, 5)
            api.create_process.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertEqual(diag.run_process(command, root, env, 5).exit_code, 0)
            self.assertTrue(marker.exists())

    def exercise_lifetime(self, mode, inherit_stdout=True, termination_error=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child.py"
            child.write_text(
                "import pathlib, sys, time\n"
                "ready = pathlib.Path(sys.argv[1])\n"
                "ready.write_text('ready')\n"
                "until = time.monotonic() + 10\n"
                "while not (ready.parent / 'release').exists() and time.monotonic() < until:\n"
                "    time.sleep(0.01)\n"
                "time.sleep(0.2 if sys.argv[2] == 'completed' else 20)\n",
                encoding="utf-8",
            )
            leader = root / "leader.py"
            leader.write_text(
                "import os, pathlib, subprocess, sys, time\n"
                "root = pathlib.Path(sys.argv[1])\n"
                "mode = sys.argv[2]\n"
                "stream = None if sys.argv[3] == 'inherit' else subprocess.DEVNULL\n"
                "child = subprocess.Popen([sys.executable, str(root / 'child.py'), "
                "str(root / 'ready'), mode], stdout=stream, stderr=stream)\n"
                "(root / 'pids.tmp').write_text(__import__('json').dumps([os.getpid(), child.pid]))\n"
                "(root / 'pids.tmp').replace(root / 'pids.json')\n"
                "until = time.monotonic() + 10\n"
                "while not (root / 'release').exists() and time.monotonic() < until:\n"
                "    time.sleep(0.01)\n"
                "if mode == 'live':\n"
                "    time.sleep(20)\n"
                "elif mode == 'completed':\n"
                "    child.wait(timeout=5)\n",
                encoding="utf-8",
            )
            env = diag.child_env(dict(os.environ), root / "home")
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env,
            )
            results = []
            errors = []
            closed_streams = []
            original_close = diag.close_process

            def close(process):
                original_close(process)
                closed_streams.append(process.stdout.closed)

            def run():
                try:
                    results.append(diag.run_process(
                        [sys.executable, str(leader), str(root), mode,
                         "inherit" if inherit_stdout else "detached"],
                        root, env, 2 if mode == "live" else 5,
                    ))
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            handles = []
            tracker = patch.object(diag, "close_process", side_effect=close)
            termination = (
                patch.object(diag, "stop_tree", side_effect=diag.DiagnosticError("command_cleanup_unknown"))
                if termination_error else contextlib.nullcontext()
            )
            try:
                with tracker, termination:
                    worker.start()
                    deadline = time.monotonic() + 5
                    while not (root / "ready").is_file() or not (root / "pids.json").is_file():
                        self.assertLess(time.monotonic(), deadline, "The real child did not start")
                        time.sleep(0.01)
                    pids = json.loads((root / "pids.json").read_text())
                    for pid in pids:
                        handle = self.kernel.OpenProcess(0x100001, False, pid)
                        self.assertTrue(handle, "Could not retain the exact test process identity")
                        handles.append(handle)
                    self.assertEqual(self.kernel.WaitForSingleObject(handles[1], 0), 258)
                    (root / "release").write_text("go", encoding="utf-8")
                    worker.join(timeout=8)
                    self.assertFalse(worker.is_alive(), "run_process did not finish within its bound")
                    if termination_error:
                        self.assertEqual(len(errors), 1)
                        self.assertIsInstance(errors[0], diag.DiagnosticError)
                        self.assertEqual(str(errors[0]), "command_cleanup_unknown")
                    else:
                        self.assertFalse(errors, errors)
                    self.assertEqual(self.kernel.WaitForSingleObject(handles[1], 1000), 0,
                                     "A descendant survived the command's return")
                    self.assertEqual(self.kernel.WaitForSingleObject(handles[0], 1000), 0)
                    self.assertIsNone(unrelated.poll(), "Unrelated same-executable process was terminated")
                    self.assertEqual(closed_streams, [True])
                    if termination_error:
                        self.assertEqual(results, [])
                        return
                    self.assertEqual(len(results), 1)
                    result = results[0]
                    if mode == "completed":
                        self.assertEqual((result.status, result.exit_code), ("succeeded", 0))
                    else:
                        self.assertEqual(result.status, "failed")
                        self.assertEqual(result.reason, "command_timeout" if mode == "live"
                                         else "command_descendants_running")
                        if mode == "exited":
                            self.assertEqual(result.exit_code, 0)
            finally:
                for handle in handles:
                    if self.kernel.WaitForSingleObject(handle, 0) == 258:
                        self.kernel.TerminateProcess(handle, 1)
                    self.kernel.WaitForSingleObject(handle, 5000)
                    self.kernel.CloseHandle(handle)
                if unrelated.poll() is None:
                    unrelated.kill()
                unrelated.wait(timeout=5)
                worker.join(timeout=5)

    def test_exited_leader_cannot_leave_stdout_descendant_alive(self):
        self.exercise_lifetime("exited")

    def test_exited_leader_cannot_leave_a_descendant_without_stdout_alive(self):
        self.exercise_lifetime("exited", inherit_stdout=False)

    def test_live_parent_timeout_and_completed_child_have_real_controls(self):
        self.exercise_lifetime("live")
        self.exercise_lifetime("completed")

    def test_job_close_still_terminates_descendants_if_explicit_termination_fails(self):
        self.exercise_lifetime("exited", termination_error=True)


class OrchestrationTests(unittest.TestCase):
    def exercise(self, original_install=1, candidate_stage_failure=None, mutate_original=False,
                 public_failure=False, npm_version=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        output = root / "evidence"
        work = root / "work"
        for path in (repo / "app" / "web", output, work):
            path.mkdir(parents=True)
        lock, metadata = fixture()
        native = metadata["packages"][diag.NATIVE]
        manifests = {"package.json": diag.encode({"dependencies": {"next": diag.VERSION}}),
                     "package-lock.json": lock_bytes(lock)}
        for name, data in manifests.items():
            (repo / "app" / "web" / name).write_bytes(data)
        files = {f"app/web/{name}": data for name, data in manifests.items()}
        files["app/web/src/lib/nativeLockCoverage.test.ts"] = b"synthetic existing guard"
        archive = archive_bytes(files)
        node = root / "toolchain" / "node.exe"
        npm = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm.parent.mkdir(parents=True)
        npm.write_bytes(b"synthetic cli")
        (npm.parent.parent / "package.json").write_bytes(diag.encode({"name": "npm", "version": diag.NPM_VERSION}))
        calls = []
        env = {key: "synthetic" for key in diag.IDENTITY_ENV}
        env["RUNNER_TEMP"] = str(root)
        source = "a" * 40

        def run(command, cwd, environment, timeout, limit=diag.OUTPUT_LIMIT):
            calls.append((command, cwd, environment, timeout))
            self.assertNotIn("NODE_OPTIONS", environment)
            self.assertLessEqual(timeout, 300)
            if command[0] == "git":
                if command[1] == "archive":
                    return diag.CommandResult("succeeded", 0, archive)
                if command[1] == "rev-parse":
                    return diag.CommandResult("succeeded", 0, (source + "\n").encode())
                return diag.CommandResult("succeeded", 0)
            if "--collect" in command:
                if public_failure:
                    return diag.CommandResult("failed", 2, diag.encode({"error": "public_tls_failure"}))
                return diag.CommandResult("succeeded", 0, diag.encode(metadata))
            if "-p" in command:
                return diag.CommandResult("succeeded", 0, diag.encode({
                    "node": diag.NODE_VERSION, "platform": "win32", "arch": "x64",
                }))
            if "--version" in command:
                return diag.CommandResult("succeeded", 0, ((npm_version or diag.NPM_VERSION) + "\n").encode())
            kind = "original" if "original" in cwd.parts else "candidate"
            if "update" in command:
                self.assertEqual(command[3], diag.NATIVE)
                self.assertIn("--package-lock-only", command)
                self.assertIn("--ignore-scripts", command)
                (cwd / "package-lock.json").write_bytes(lock_bytes(add_record(lock, native)))
                return diag.CommandResult("succeeded", 0)
            if "ci" in command:
                self.assertIn("--ignore-scripts", command)
                code = original_install if kind == "original" else 0
                return diag.CommandResult("failed" if code else "succeeded", code, b"npm error code EUSAGE\n" if code else b"")
            if any(value.endswith("_web_native_probe.cjs") for value in command):
                if candidate_stage_failure == "native":
                    return diag.CommandResult("succeeded", 0, b'{"native":true}')
                return diag.CommandResult("succeeded", 0, native_result(native))
            if kind == "original":
                return diag.CommandResult("failed", 1)
            if mutate_original and "build" in command:
                (work / "original" / "app" / "web" / "package-lock.json").write_bytes(b"changed")
            code = 7 if candidate_stage_failure and any(candidate_stage_failure in value for value in command) else 0
            return diag.CommandResult("failed" if code else "succeeded", code)

        diagnostic = diag.Diagnostic(output, env)
        with (
            patch.object(diag, "ROOT", repo),
            patch.object(diag, "ORIGINAL_HASHES", {name: diag.sha256(data) for name, data in manifests.items()}),
            patch.object(diag, "identity", return_value={"sourceCommit": source}),
            patch.object(diag.shutil, "which", return_value=str(node)),
            patch.object(diag, "run_process", side_effect=run),
        ):
            try:
                diagnostic.run(work)
            except diag.DiagnosticError as exc:
                diagnostic.failure(str(exc))
            try:
                diagnostic.finish()
            except diag.DiagnosticError as exc:
                diagnostic.failure(str(exc))
            diagnostic.save()
        return diagnostic.report, calls, output

    def test_candidate_success_does_not_green_the_failed_original(self):
        report, calls, output = self.exercise()
        self.assertEqual(report["status"], "candidate_verified_original_failed")
        self.assertTrue(report["candidateValidated"])
        self.assertFalse(report["originalValidated"])
        self.assertFalse(report["adoptionAuthorized"])
        self.assertEqual(report["stages"]["original_install"]["exitCode"], 1)
        self.assertEqual(report["stages"]["original_native"]["status"], "not_run")
        self.assertEqual(report["stages"]["candidate_native"]["status"], "succeeded")
        self.assertEqual(report["stages"]["candidate_build"]["exitCode"], 0)
        self.assertEqual(len(report["commands"]), len(calls))
        self.assertTrue(all("exitCode" in command for command in report["commands"]))
        self.assertEqual({path.name for path in output.iterdir()},
                         {"report.json", "candidate-package-lock.json", "candidate-lock.diff"})
        self.assertLessEqual(sum(path.stat().st_size for path in output.iterdir()), 2 * diag.LOCK_LIMIT)
        self.assertNotIn("scripts", json.dumps([command for command, *_ in calls if command[0] == "git"]))

    def test_original_native_control_runs_only_after_its_own_successful_install(self):
        report, _, _ = self.exercise(original_install=0)
        self.assertEqual(report["stages"]["original_guard"]["status"], "failed")
        self.assertEqual(report["stages"]["original_native"]["status"], "succeeded")
        self.assertFalse(report["originalValidated"])
        self.assertTrue(report["candidateValidated"])

    def test_zero_exit_without_native_proof_blocks_all_later_candidate_execution(self):
        report, _, _ = self.exercise(candidate_stage_failure="native")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["candidateValidated"])
        self.assertEqual(report["stages"]["candidate_native"]["status"], "failed")
        self.assertEqual(report["stages"]["candidate_tests"]["status"], "not_run")
        self.assertEqual(report["stages"]["candidate_build"]["status"], "not_run")

    def test_lint_failure_and_original_mutation_remain_visible(self):
        report, _, _ = self.exercise(candidate_stage_failure="eslint")
        self.assertEqual(report["stages"]["candidate_lint"]["exitCode"], 7)
        self.assertFalse(report["candidateValidated"])
        self.assertEqual(report["stages"]["candidate_build"]["status"], "succeeded")
        report, _, _ = self.exercise(mutate_original=True)
        self.assertEqual(report["stages"]["immutability"]["status"], "failed")
        self.assertFalse(report["candidateValidated"])
        self.assertIn("original_or_candidate_mutated", json.dumps(report["errors"]))

    def test_missing_public_evidence_or_wrong_npm_never_starts_package_resolution(self):
        for arguments in ({"public_failure": True}, {"npm_version": "99.0.0"}):
            report, calls, _ = self.exercise(**arguments)
            self.assertFalse(report["candidateValidated"])
            self.assertEqual(report["stages"]["candidate_lock"]["status"], "not_run")
            self.assertFalse(any("ci" in command or "update" in command for command, *_ in calls))
        report, calls, _ = self.exercise()
        self.assertTrue(report["candidateValidated"])
        self.assertTrue(any("update" in command for command, *_ in calls))

@unittest.skipUnless(sys.platform == "win32", "The native-probe contract runs in the Windows diagnostic job")
class NativeProbeContractTests(unittest.TestCase):
    def run_probe(self, main="synthetic.node", expected_hash=None):
        node = os.environ.get("AI4IA_TEST_NODE") or shutil.which("node")
        self.assertIsNotNone(node, "Node22 must be supplied by setup-node or the explicit offline test path")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / "web"
            package = web / "node_modules" / "@next" / "swc-win32-x64-msvc"
            package.mkdir(parents=True)
            (web / "package.json").write_text("{}", encoding="utf-8")
            (package / "package.json").write_text(json.dumps({
                "name": diag.NATIVE, "version": diag.VERSION, "cpu": ["x64"], "os": ["win32"], "main": main,
            }), encoding="utf-8")
            implementation = b'module.exports = { transformSync: () => ({ code: "exports.answer = 42;" }) };'
            (package / main).write_bytes(implementation)
            loader = root / "synthetic-loader.cjs"
            loader.write_text(
                'const assert = require("node:assert/strict");\n'
                'require("node:module")._extensions[".node"] = (m) => {\n'
                '  m.exports = { transformSync: (source, isModule, options) => {\n'
                '    assert.equal(source, "export const answer: number = 42;");\n'
                '    assert.equal(isModule, false);\n'
                '    assert.equal(JSON.parse(options).jsc.parser.syntax, "typescript");\n'
                '    return { code: "exports.answer = 42;" };\n'
                '  }};\n'
                '};\n', encoding="utf-8",
            )
            return subprocess.run([
                node, "--require", str(loader), str(PROBE), str(web),
                expected_hash or hashlib.sha256(implementation).hexdigest(),
            ], env=diag.child_env(dict(os.environ), root / "node-home"),
                capture_output=True, timeout=10, check=False)

    def test_probe_enters_native_loader_and_transforms_but_rejects_a_js_fallback(self):
        passed = self.run_probe()
        self.assertEqual(passed.returncode, 0, passed.stderr.decode())
        self.assertEqual(json.loads(passed.stdout)["result"], 42)
        fallback = self.run_probe(main="fallback.cjs")
        self.assertNotEqual(fallback.returncode, 0)
        self.assertEqual(fallback.stdout, b"")
        self.assertIn(b"native_control_failed", fallback.stderr)
        self.assertEqual(self.run_probe().returncode, 0)

    def test_probe_rejects_bytes_not_matching_the_verified_tarball_member(self):
        self.assertEqual(self.run_probe().returncode, 0)
        self.assertNotEqual(self.run_probe(expected_hash="0" * 64).returncode, 0)


if __name__ == "__main__":
    unittest.main()
