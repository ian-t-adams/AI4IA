"""Offline controls. Synthetic native-loader fixtures are NOT Windows SWC evidence."""
from __future__ import annotations

import base64
import contextlib
import copy
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
import unittest
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
