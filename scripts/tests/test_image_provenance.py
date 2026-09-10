"""Exercise the real release gate and deployment commands with offline tool stubs.

Fixtures follow actions/attest v4.2.2, Trivy 0.71.2 SPDX 2.3, and gh 2.100.0's
Sigstore verification-result schema. Stubbed cryptography is not live signing
evidence: these tests establish local policy, command, coverage and ordering.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.tests._loader import load_script
from scripts.tests.test_immutable_image_promotion import (
    BASH,
    BUILD_STEP,
    DEPLOY_STEP,
    ROOT,
    _services,
    _step,
    _step_index,
    _steps,
)

SCRIPT = ROOT / "scripts" / "verify-image-provenance.py"
with patch("sys.path", [str(SCRIPT.parent), *sys.path]):
    provenance = load_script("image_provenance", SCRIPT, register=True)

REPOSITORY = "ian-t-adams/AI4IA"
COMMIT = "1234567890abcdef1234567890abcdef12345678"
WORKFLOW_ID = f"https://github.com/{REPOSITORY}/.github/workflows/deploy.yml@refs/heads/main"
INVOCATION = f"https://github.com/{REPOSITORY}/actions/runs/123456789/attempts/1"
IMAGES = {
    service: f"crai4ia1234.azurecr.io/ai4ia/{service}-prod@sha256:{character * 64}"
    for service, character in (("web", "a"), ("api", "b"), ("proxy", "c"))
}
ENVIRONMENT = {
    "GITHUB_REPOSITORY": REPOSITORY,
    "GITHUB_SHA": COMMIT,
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_WORKFLOW_REF": WORKFLOW_ID.removeprefix("https://github.com/"),
    "GITHUB_WORKFLOW_SHA": COMMIT,
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "123456789",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_EVENT_NAME": "push",
    "AZURE_ENV_NAME": "prod",
    "GH_TOKEN": "offline-test-token-not-a-credential",
}
PREPARE = "Bind production image subjects"
GENERATE = "Generate production SPDX SBOMs"
VERIFY = "Verify production image attestations"


def spdx(image: str) -> dict:
    return {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "dataLicense": "CC0-1.0",
        "name": image,
        "documentNamespace": "http://trivy.dev/container_image/synthetic-1234",
        "creationInfo": {
            "created": "2026-09-09T23:16:00Z",
            "creators": ["Organization: aquasecurity", "Tool: trivy-0.71.2"],
        },
        "packages": [
            {
                "SPDXID": "SPDXRef-ContainerImage-1234", "name": image,
                "primaryPackagePurpose": "CONTAINER", "downloadLocation": "NONE",
                "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
            },
            {
                "SPDXID": "SPDXRef-Package-5678", "name": "synthetic-library",
                "versionInfo": "1.2.3", "primaryPackagePurpose": "LIBRARY",
                "downloadLocation": "NONE", "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
            },
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-ContainerImage-1234",
            },
            {
                "spdxElementId": "SPDXRef-ContainerImage-1234", "relationshipType": "CONTAINS",
                "relatedSpdxElement": "SPDXRef-Package-5678",
            },
        ],
    }


def verified(image: str, kind: str, document: dict) -> list[dict]:
    name, digest = image.split("@sha256:")
    predicate = {
        "buildDefinition": {
            "buildType": "https://actions.github.io/buildtypes/workflow/v1",
            "externalParameters": {"workflow": {
                "ref": "refs/heads/main", "repository": f"https://github.com/{REPOSITORY}",
                "path": ".github/workflows/deploy.yml",
            }},
            "internalParameters": {"github": {
                "event_name": "push", "repository_id": "1234",
                "repository_owner_id": "5678", "runner_environment": "github-hosted",
            }},
            "resolvedDependencies": [{
                "uri": f"git+https://github.com/{REPOSITORY}@refs/heads/main",
                "digest": {"gitCommit": COMMIT},
            }],
        },
        "runDetails": {"builder": {"id": WORKFLOW_ID}, "metadata": {"invocationId": INVOCATION}},
    } if kind == "provenance" else document
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": name, "digest": {"sha256": digest}}],
        "predicateType": "https://slsa.dev/provenance/v1" if kind == "provenance"
        else "https://spdx.dev/Document/v2.3",
        "predicate": predicate,
    }
    bundle = {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {"certificate": {"rawBytes": "U1lOVEhFVElDLUNFUlQ="}},
        "dsseEnvelope": {
            "payloadType": "application/vnd.in-toto+json",
            "payload": base64.b64encode(json.dumps(statement).encode()).decode(),
            "signatures": [{"sig": "U1lOVEhFVElDLVNJRw=="}],
        },
    }
    return [{
        "attestation": {"bundle": bundle},
        "verificationResult": {
            "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
            "signature": {"certificate": {
                "subjectAlternativeName": WORKFLOW_ID,
                "extensions": {
                    "issuer": "https://token.actions.githubusercontent.com",
                    "sourceRepositoryURI": f"https://github.com/{REPOSITORY}",
                    "sourceRepositoryDigest": COMMIT,
                    "sourceRepositoryRef": "refs/heads/main",
                    "buildSignerURI": WORKFLOW_ID,
                    "buildSignerDigest": COMMIT,
                    "buildConfigURI": WORKFLOW_ID,
                    "buildConfigDigest": COMMIT,
                    "runnerEnvironment": "github-hosted",
                    "runInvocationURI": INVOCATION,
                    "buildTrigger": "push",
                },
            }},
            "verifiedTimestamps": [{
                "type": "TransparencyLog", "uri": "https://rekor.sigstore.dev",
                "timestamp": "2026-09-09T23:16:00Z",
            }],
            "statement": statement,
        },
    }]


# These stubs validate the command contract, but deliberately do NOT validate the
# response fixture. Flipping one signed field must exercise our real policy code.
TOOL_STUB = r'''
import json
import os
import sys
from pathlib import Path

tool, args = sys.argv[1], sys.argv[2:]
root = Path(os.environ["FAKE_ROOT"])
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": tool, "args": args}) + "\n")
images = json.loads((root / "images.json").read_text())

def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(19)

if tool == "azd":
    if len(args) != 5 or args[:1] != ["deploy"] or args[2] != "--from-package":
        fail("unexpected deployment")
    if args[3] != images[args[1]] or args[4] != "--no-prompt":
        fail("deployment changed the image reference")
    raise SystemExit(0)

if tool == "trivy":
    image = args[-1]
    service = next(service for service, reference in images.items() if reference == image)
    if args[:6] != ["image", "--image-src", "remote", "--format", "spdx-json", "--list-all-pkgs"]:
        fail("scanner must read registry bytes as SPDX")
    if os.environ.get("FAKE_FAILURE") == "trivy:" + service:
        fail("sensitive-stub-diagnostic")
    output = Path(args[args.index("--output") + 1])
    output.write_bytes((root / (service + ".spdx.json")).read_bytes())
    raise SystemExit(0)

if tool != "gh":
    fail("unexpected executable")
if args == ["--version"]:
    print("gh version " + os.environ.get("FAKE_GH_VERSION", "2.100.0") + " (2026-09-01)")
    raise SystemExit(0)
if args[:2] != ["attestation", "verify"]:
    fail("only attestation verification is supported")
options = args[3:]
bundle = Path(options[options.index("--bundle") + 1])
service, kind, *_ = bundle.name.split(".")
required = [
    "--bundle", str(bundle), "--repo", "ian-t-adams/AI4IA",
    "--cert-identity", os.environ["GITHUB_WORKFLOW_REF"],
]
required[required.index("--cert-identity") + 1] = "https://github.com/" + os.environ["GITHUB_WORKFLOW_REF"]
required += [
    "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
    "--deny-self-hosted-runners", "--source-ref", "refs/heads/main",
    "--source-digest", os.environ["GITHUB_SHA"], "--signer-digest", os.environ["GITHUB_SHA"],
    "--predicate-type", "https://slsa.dev/provenance/v1" if kind == "provenance"
    else "https://spdx.dev/Document/v2.3",
    "--hostname", "github.com", "--format", "json",
]
if options != required or args[2] != "oci://" + images[service] or not bundle.is_file():
    fail("missing or changed cryptographic verification criteria")
if os.environ.get("FAKE_FAILURE") == service + ":" + kind:
    sys.stdout.buffer.write((root / (service + "." + kind + ".json")).read_bytes())
    sys.stdout.buffer.flush()
    fail("sensitive-stub-diagnostic")
sys.stdout.buffer.write((root / (service + "." + kind + ".json")).read_bytes())
'''


def tool_stub(directory: Path, tool: str, implementation: Path) -> None:
    target = directory / tool
    target.write_text(
        "#!/usr/bin/env bash\nexec "
        + shlex.quote(Path(sys.executable).as_posix()) + " "
        + shlex.quote(implementation.as_posix()) + " " + tool + ' "$@"\n',
        encoding="utf-8", newline="\n",
    )
    target.chmod(0o755)
    if os.name == "nt":
        (directory / f"{tool}.cmd").write_text(
            f'@"{sys.executable}" "{implementation}" {tool} %*\n', encoding="utf-8",
        )


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class ReleaseGateBehaviorTests(unittest.TestCase):
    def flow(self, *, change=None, overrides=None, missing_bundle="", after_verify=""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = {
                service: {
                    kind: verified(image, kind, spdx(image)) for kind in ("provenance", "sbom")
                } for service, image in IMAGES.items()
            }
            documents = {service: spdx(image) for service, image in IMAGES.items()}
            if change:
                change(fixture, documents)
            (root / "images.json").write_text(json.dumps(IMAGES), encoding="utf-8")
            env = {**os.environ, **ENVIRONMENT}
            env.update(
                RUNNER_TEMP=root.as_posix(), FAKE_ROOT=str(root),
                GITHUB_OUTPUT=str(root / "outputs"), GITHUB_STEP_SUMMARY=str(root / "summary"),
                **{f"{service.upper()}_IMAGE": image for service, image in IMAGES.items()},
            )
            for service in IMAGES:
                (root / f"{service}.spdx.json").write_text(json.dumps(documents[service]), encoding="utf-8")
                for kind in ("provenance", "sbom"):
                    response = fixture[service][kind]
                    (root / f"{service}.{kind}.json").write_bytes(
                        response if isinstance(response, bytes) else json.dumps(response).encode()
                    )
                    bundle = root / f"action-{service}-{kind}.json"
                    # This is a generated-action fixture, separate from gh's response.
                    original = verified(IMAGES[service], kind, documents[service])
                    bundle.write_text(json.dumps(original[0]["attestation"]["bundle"]), encoding="utf-8")
                    key = f"{service.upper()}_{kind.upper()}_BUNDLE"
                    env[key] = "" if missing_bundle == key else str(bundle)
            env.update(overrides or {})
            bin_dir = root / "bin"
            bin_dir.mkdir()
            implementation = root / "tool.py"
            implementation.write_text(TOOL_STUB, encoding="utf-8")
            for tool in ("gh", "trivy", "azd"):
                tool_stub(bin_dir, tool, implementation)
            python_wrapper = bin_dir / "python"
            python_wrapper.write_text(
                "#!/usr/bin/env bash\nexec " + shlex.quote(Path(sys.executable).as_posix()) + ' "$@"\n',
                encoding="utf-8", newline="\n",
            )
            python_wrapper.chmod(0o755)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            # MSYS prepends its own tools at shell startup; prepend the stubs
            # afterwards so an installed curl/gh/azd cannot escape the fixture.
            run = f'export PATH="$(cd {shlex.quote(bin_dir.as_posix())} && pwd):$PATH"\n'
            run += "\n".join(_step(name)["run"] for name in (PREPARE, GENERATE, VERIFY))
            run += '\nIMAGE_PROOF_SHA256="$(sed -n \'s/^proof_sha256=//p\' "$GITHUB_OUTPUT")"\n'
            run += after_verify + "\n" + _step(DEPLOY_STEP)["run"]
            script = root / "release.sh"
            script.write_text(run, encoding="utf-8", newline="\n")
            result = subprocess.run([BASH, str(script)], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60)
            calls_file = root / "calls.jsonl"
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
            output_file = root / "outputs"
            outputs = dict(line.split("=", 1) for line in output_file.read_text().splitlines()) if output_file.exists() else {}
            evidence = root / "ai4ia-image-evidence"
            proof_file = evidence / "verified-images.json"
            proof = json.loads(proof_file.read_text()) if proof_file.exists() else None
            return result, calls, outputs, proof

    def assert_denied(self, **kwargs):
        result, calls, outputs, proof = self.flow(**kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse([call for call in calls if call["tool"] == "azd"], calls)
        self.assertNotIn("proof_sha256", outputs)
        self.assertIsNone(proof)
        return result, calls

    def test_all_six_proofs_allow_only_the_original_three_images_to_deploy(self):
        result, calls, outputs, proof = self.flow()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        scans = [call for call in calls if call["tool"] == "trivy"]
        verifications = [call for call in calls if call["tool"] == "gh" and call["args"][0] == "attestation"]
        deploys = [call for call in calls if call["tool"] == "azd"]
        self.assertEqual(len(scans), len(_services()))
        self.assertEqual(len(verifications), 2 * len(_services()))
        self.assertEqual([call["args"][1] for call in deploys], ["web", "api", "proxy"])
        self.assertLess(max(calls.index(call) for call in verifications), calls.index(deploys[0]))
        self.assertEqual(proof["images"], IMAGES)
        self.assertEqual(set(proof["files"]), set(provenance.evidence_names()))
        self.assertRegex(outputs["proof_sha256"], r"^[0-9a-f]{64}$")

    def test_each_subject_and_predicate_mismatch_denies_before_any_deploy(self):
        for kind in ("provenance", "sbom"):
            for field in ("digest", "image-name", "predicate-type", "missing-subject", "extra-subject", "predicate"):
                def change(fixture, _documents):
                    statement = fixture["proxy"][kind][0]["verificationResult"]["statement"]
                    if field == "digest":
                        statement["subject"][0]["digest"]["sha256"] = "f" * 64
                    elif field == "image-name":
                        statement["subject"][0]["name"] = "foreign.azurecr.io/ai4ia/proxy-prod"
                    elif field == "predicate-type":
                        statement["predicateType"] = "https://example.org/not-provenance"
                    elif field == "missing-subject":
                        statement["subject"] = []
                    elif field == "extra-subject":
                        statement["subject"].append(copy.deepcopy(statement["subject"][0]))
                    else:
                        statement["predicate"] = {}
                with self.subTest(kind=kind, field=field):
                    self.assert_denied(change=change)

    def test_every_certificate_identity_boundary_rejects_the_same_fixture(self):
        changes = {
            "issuer": "https://issuer.example.org",
            "sourceRepositoryURI": "https://github.com/foreign/AI4IA",
            "sourceRepositoryDigest": "f" * 40,
            "sourceRepositoryRef": "refs/heads/unreviewed",
            "buildSignerURI": WORKFLOW_ID.replace("deploy.yml", "pull-request.yml"),
            "buildSignerDigest": "f" * 40,
            "buildConfigURI": WORKFLOW_ID.replace("deploy.yml", "other.yml"),
            "buildConfigDigest": "e" * 40,
            "runnerEnvironment": "self-hosted",
            "runInvocationURI": INVOCATION.replace("/attempts/1", "/attempts/2"),
            "buildTrigger": "pull_request",
        }
        for field, wrong in changes.items():
            for value in (wrong, None):
                def change(fixture, _documents):
                    extensions = fixture["proxy"]["sbom"][0]["verificationResult"]["signature"]["certificate"]["extensions"]
                    if value is None:
                        del extensions[field]
                    else:
                        extensions[field] = value
                with self.subTest(field=field, missing=value is None):
                    self.assert_denied(change=change)

    def test_san_timestamps_and_result_shape_are_not_optional(self):
        for field in ("san", "timestamps", "signature", "result", "empty", "extra", "malformed", "duplicates"):
            def change(fixture, _documents):
                response = fixture["api"]["provenance"]
                result = response[0]["verificationResult"]
                if field == "san":
                    result["signature"]["certificate"]["subjectAlternativeName"] += "-other"
                elif field == "timestamps":
                    result["verifiedTimestamps"] = []
                elif field == "signature":
                    result["signature"] = {}
                elif field == "result":
                    del response[0]["verificationResult"]
                elif field == "empty":
                    fixture["api"]["provenance"] = []
                elif field == "extra":
                    response.append(copy.deepcopy(response[0]))
                elif field == "malformed":
                    fixture["api"]["provenance"] = b'not JSON sensitive-stub-diagnostic'
                else:
                    fixture["api"]["provenance"] = b'[{"verificationResult": {}, "verificationResult": {}}]'
            with self.subTest(field=field):
                result, _ = self.assert_denied(change=change)
                self.assertNotIn("sensitive-stub-diagnostic", result.stdout + result.stderr)

    def test_signed_spdx_and_slsa_content_must_match_the_scanned_image_and_build(self):
        for kind, field in (("sbom", "package-version"), ("provenance", "dependency"),
                            ("provenance", "workflow"), ("provenance", "invocation")):
            def change(fixture, _documents):
                predicate = fixture["web"][kind][0]["verificationResult"]["statement"]["predicate"]
                if field == "package-version":
                    predicate["packages"][1]["versionInfo"] = "9.9.9"
                elif field == "dependency":
                    predicate["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"] = "f" * 40
                elif field == "workflow":
                    predicate["buildDefinition"]["externalParameters"]["workflow"]["path"] = ".github/workflows/pr.yml"
                else:
                    predicate["runDetails"]["metadata"]["invocationId"] = INVOCATION + "0"
            with self.subTest(kind=kind, field=field):
                self.assert_denied(change=change)

    def test_missing_bundle_for_any_service_never_emits_a_partial_proof(self):
        for service in _services():
            for kind in ("provenance", "sbom"):
                with self.subTest(service=service, kind=kind):
                    self.assert_denied(missing_bundle=f"{service.upper()}_{kind.upper()}_BUNDLE")

    def test_missing_image_for_any_service_stops_before_egress(self):
        for service in _services():
            with self.subTest(service=service):
                _, calls = self.assert_denied(overrides={f"{service.upper()}_IMAGE": ""})
                self.assertEqual(calls, [])

    def test_cli_failure_is_not_unsigned_success_and_diagnostics_are_not_retained(self):
        for failure in ("web:provenance", "api:sbom", "proxy:sbom"):
            with self.subTest(failure=failure):
                result, _ = self.assert_denied(overrides={"FAKE_FAILURE": failure})
                self.assertNotIn("sensitive-stub-diagnostic", result.stdout + result.stderr)
        self.assert_denied(overrides={"FAKE_GH_VERSION": "2.98.0"})
        self.assert_denied(overrides={"GH_TOKEN": ""})

    def test_scanner_failure_or_an_empty_or_crossed_sbom_stops_before_signing(self):
        self.assert_denied(overrides={"FAKE_FAILURE": "trivy:api"})
        for field in ("empty", "version", "name", "disconnected", "wrong-tool", "duplicate-id"):
            def change(_fixture, documents):
                document = documents["proxy"]
                if field == "empty":
                    document["packages"] = []
                elif field == "version":
                    document["spdxVersion"] = "SPDX-2.2"
                elif field == "name":
                    document["name"] = IMAGES["api"]
                elif field == "disconnected":
                    document["relationships"] = document["relationships"][:1]
                elif field == "wrong-tool":
                    document["creationInfo"]["creators"] = ["Tool: unknown"]
                else:
                    document["packages"].append(copy.deepcopy(document["packages"][1]))
            with self.subTest(field=field):
                _, calls = self.assert_denied(change=change)
                self.assertFalse([call for call in calls if call["tool"] == "gh"])

    def test_source_and_workflow_ref_must_be_the_admitted_main_run(self):
        for name, value in (
            ("GITHUB_REF", "refs/heads/pr"), ("GITHUB_WORKFLOW_SHA", "f" * 40),
            ("GITHUB_WORKFLOW_REF", ENVIRONMENT["GITHUB_WORKFLOW_REF"] + "-evil"),
            ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_SERVER_URL", "https://github.example.org"),
        ):
            with self.subTest(name=name):
                _, calls = self.assert_denied(overrides={name: value})
                self.assertEqual(calls, [])

    def test_changed_output_or_evidence_cannot_reuse_a_successful_proof(self):
        for alteration in (
            f"WEB_IMAGE='{IMAGES['web'].replace('a' * 64, 'f' * 64)}'",
            "IMAGE_PROOF_SHA256=''",
            'printf " " >> "$RUNNER_TEMP/ai4ia-image-evidence/verified-images.json"',
            'printf " " >> "$RUNNER_TEMP/ai4ia-image-evidence/api.spdx.json"',
            'printf " " >> "$RUNNER_TEMP/ai4ia-image-evidence/proxy.sbom.sigstore.json"',
            'printf " " >> "$RUNNER_TEMP/ai4ia-image-evidence/web.provenance.verification.json"',
        ):
            with self.subTest(alteration=alteration):
                result, calls, outputs, _proof = self.flow(after_verify=alteration)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("proof_sha256", outputs, "the verification path was never reached")
                self.assertFalse([call for call in calls if call["tool"] == "azd"])


class EvidenceInputAndBoundsTests(unittest.TestCase):
    def test_release_reference_parser_is_strict_without_changing_legacy_rollout_parsing(self):
        values = [f"{service}={reference}" for service, reference in IMAGES.items()]
        self.assertEqual(provenance.release_images(values, "prod"), IMAGES)
        self.assertEqual(provenance.parse_expected_images(["web=acr.azurecr.io/web:old"]),
                         {"web": "acr.azurecr.io/web:old"})
        for invalid in (
            values[:-1], values + [values[0]], [],
            [values[0].replace("web-prod", "api-prod"), *values[1:]],
            [values[0].replace("crai4ia1234.azurecr.io", "crai4ia1234.azurecr.io.evil.test"), *values[1:]],
            [values[0].replace("crai4ia1234.azurecr.io", "other.azurecr.io"), *values[1:]],
            [values[0].replace("@sha256:" + "a" * 64, ":latest"), *values[1:]],
            [values[0].replace("a" * 64, "a" * 63), *values[1:]],
            [values[0] + "\n", *values[1:]],
            ["web=secret@unapproved.example/x@sha256:" + "a" * 64, *values[1:]],
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(provenance.ProvenanceError):
                    provenance.release_images(invalid, "prod")

    def test_json_and_file_bounds_do_not_accept_truncated_or_ambiguous_evidence(self):
        self.assertEqual(provenance.strict_json(b'{"n":1}'), {"n": 1})
        for invalid in (b'{"n":1,"n":2}', b'{"n":NaN}', b'{"n":Infinity}', b'{"n":1e999}', b"\xff",
                        b"[" * 60 + b"0" + b"]" * 60, b'{"n":'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(provenance.ProvenanceError):
                    provenance.strict_json(invalid)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence"
            path.write_bytes(b"x" * 16)
            self.assertEqual(provenance.read_bytes(path, 16), b"x" * 16)
            with self.assertRaises(provenance.ProvenanceError):
                provenance.read_bytes(path, 15)
            path.write_bytes(b"")
            with self.assertRaises(provenance.ProvenanceError):
                provenance.read_bytes(path, 16)

    def test_actual_child_output_error_and_time_bounds(self):
        good = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1024)"]
        self.assertEqual(len(provenance.run_bounded(good, limit=1024, timeout=5)), 1024)
        with self.assertRaises(provenance.ProvenanceError):
            provenance.run_bounded(good, limit=1023, timeout=5)
        with self.assertRaisesRegex(provenance.ProvenanceError, "timed out"):
            provenance.run_bounded([sys.executable, "-c", "import time; time.sleep(10)"],
                                   limit=1024, timeout=0.1)
        with self.assertRaisesRegex(provenance.ProvenanceError, "verifier failed"):
            provenance.run_bounded([sys.executable, "-c", "raise SystemExit(3)"], limit=1024, timeout=5)


class ImageAttestationWorkflowTests(unittest.TestCase):
    def test_shared_inventory_and_project_match_azure_yaml(self):
        document = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(provenance.SERVICES), set(document["services"]))
        self.assertEqual(provenance.PROJECT, document["name"])

    def test_every_service_is_scanned_and_attested_twice_before_verification_and_deploy(self):
        names = (BUILD_STEP, PREPARE, GENERATE, VERIFY, "Retain production image evidence", DEPLOY_STEP)
        indexes = [_step_index(name) for name in names]
        self.assertEqual(indexes, sorted(indexes))
        attestations = [step for step in _steps() if step.get("uses", "").startswith("actions/attest@")]
        self.assertEqual(len(attestations), 2 * len(_services()))
        for service in _services():
            for kind in ("provenance", "sbom"):
                matches = [step for step in attestations if step.get("id") == f"{service}_{kind}"]
                self.assertEqual(len(matches), 1)
                step = matches[0]
                self.assertEqual(step["uses"], "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6")
                inputs = step["with"]
                self.assertEqual(inputs["subject-name"], "${{ steps.subjects.outputs." + service + "_name }}")
                self.assertEqual(inputs["subject-digest"], "${{ steps.subjects.outputs." + service + "_digest }}")
                self.assertIs(inputs["push-to-registry"], True)
                self.assertIs(inputs["create-storage-record"], False)
                if kind == "sbom":
                    self.assertEqual(inputs["sbom-path"], "${{ runner.temp }}/ai4ia-image-evidence/" + service + ".spdx.json")
                else:
                    self.assertNotIn("sbom-path", inputs)
                self.assertFalse(set(inputs) & {"subject-path", "subject-checksums", "predicate", "predicate-path"})
                self.assertNotIn("if", step)
                self.assertNotIn("continue-on-error", step)
                self.assertLess(_step_index(GENERATE), _step_index(step["name"]))
                self.assertLess(_step_index(step["name"]), _step_index(VERIFY))
                self.assertEqual(_step(VERIFY)["env"][f"{service.upper()}_{kind.upper()}_BUNDLE"],
                                 "${{ steps." + service + "_" + kind + ".outputs.bundle-path }}")
            for name in (PREPARE, GENERATE, VERIFY, DEPLOY_STEP):
                self.assertEqual(_step(name)["env"][f"{service.upper()}_IMAGE"],
                                 "${{ steps.images.outputs." + service + "_image }}")
        self.assertEqual(_step(DEPLOY_STEP)["env"]["IMAGE_PROOF_SHA256"],
                         "${{ steps.image_proofs.outputs.proof_sha256 }}")
        self.assertEqual(_step(VERIFY)["env"]["GH_TOKEN"], "${{ github.token }}")

    def test_fail_closed_workflow_wiring_and_retention(self):
        for name in (PREPARE, GENERATE, VERIFY, DEPLOY_STEP):
            step = _step(name)
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("if", step)
            self.assertTrue(step["run"].startswith("set -euo pipefail\n"))
        deploy = _step(DEPLOY_STEP)["run"]
        self.assertLess(deploy.index("verify-image-provenance.py authorize"), deploy.index("azd deploy "))
        artifact = _step("Retain production image evidence")
        self.assertEqual(artifact["if"], "${{ always() && steps.subjects.outcome == 'success' }}")
        self.assertEqual(artifact["with"]["retention-days"], 30)
        self.assertEqual(artifact["with"]["if-no-files-found"], "error")
        self.assertEqual(artifact["with"]["path"], "${{ runner.temp }}/ai4ia-image-evidence/")
        quality = (ROOT / ".github/workflows/quality.yml").read_text()
        self.assertIn("scripts.tests.test_image_provenance", quality)

    def test_evidence_tools_are_pinned_by_reviewed_release_checksum_before_provision(self):
        step = _step("Install pinned image evidence tools")
        env = step["env"]
        self.assertEqual(env["GH_VERSION"], provenance.GH_VERSION)
        self.assertEqual(env["TRIVY_VERSION"], provenance.TRIVY_VERSION)
        self.assertEqual(env["GH_ARCHIVE_SHA256"], "e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be")
        self.assertEqual(env["TRIVY_ARCHIVE_SHA256"], "0510e71e2fd39bf863856d499c8dc19feb4e7336546394c502a8f5cc7ab27460")
        run = step["run"]
        self.assertEqual(run.count("sha256sum --check --strict"), 2)
        self.assertNotIn("releases/latest", run)
        for tool in ("gh", "trivy"):
            self.assertLess(run.index(f'"${tool.upper()}_ARCHIVE_SHA256"'),
                            run.index(f'tar -xzf "$tools/{tool}.tar.gz"'))
        self.assertLess(_step_index(step["name"]), _step_index("Capture pre-provision revisions"))


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class EvidenceToolInstallerTests(unittest.TestCase):
    def install(self, *, corrupt="", download_error=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            step = _step("Install pinned image evidence tools")
            env = {**os.environ, **step["env"], "RUNNER_TEMP": root.as_posix(),
                   "GITHUB_PATH": str(root / "github-path")}
            for tool, member, output in (
                ("gh", "gh_2.100.0_linux_amd64/bin/gh", "gh version 2.100.0 (2026-09-01)"),
                ("trivy", "trivy", "Version: 0.71.2"),
            ):
                data = f"#!/usr/bin/env bash\nprintf '%s\\n' '{output}'\n".encode()
                archive = root / f"source-{tool}.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    info = tarfile.TarInfo(member)
                    info.mode, info.size = 0o755, len(data)
                    tar.addfile(info, io.BytesIO(data))
                # The real checksum program checks synthetic release archives;
                # the wiring test independently locks the real release hashes.
                env[f"{tool.upper()}_ARCHIVE_SHA256"] = (
                    "f" * 64 if corrupt == tool else hashlib.sha256(archive.read_bytes()).hexdigest()
                )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            curl = bin_dir / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\n"
                + ("exit 22\n" if download_error else "")
                + 'while [ "$1" != "--output" ]; do shift; done\n'
                + 'output="$2"; url="$3"\n'
                + 'case "$url" in\n'
                + '  https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_linux_amd64.tar.gz) tool=gh ;;\n'
                + '  https://github.com/aquasecurity/trivy/releases/download/v0.71.2/trivy_0.71.2_Linux-64bit.tar.gz) tool=trivy ;;\n'
                + '  *) exit 23 ;;\n'
                + 'esac\ncp "$RUNNER_TEMP/source-$tool.tar.gz" "$output"\n',
                encoding="utf-8", newline="\n",
            )
            curl.chmod(0o755)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            script = root / "install.sh"
            prefix = f'export PATH="$(cd {shlex.quote(bin_dir.as_posix())} && pwd):$PATH"\n'
            # GNU tar treats a drive-letter archive as a remote host. The
            # installer runs on Linux; use MSYS's equivalent path in this test.
            prefix += 'export RUNNER_TEMP="$(cd "$RUNNER_TEMP" && pwd)"\n'
            script.write_text(prefix + step["run"], encoding="utf-8", newline="\n")
            result = subprocess.run([BASH, str(script)], env=env, cwd=ROOT,
                                    capture_output=True, text=True, timeout=30)
            path_file = root / "github-path"
            paths = path_file.read_text().splitlines() if path_file.exists() else []
            tools = root / "ai4ia-image-tools"
            return result, paths, (tools / "gh_2.100.0_linux_amd64/bin/gh").exists(), (tools / "trivy").exists()

    def test_matching_checksums_allow_only_the_pinned_tools_onto_path(self):
        result, paths, gh_exists, trivy_exists = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(paths), 2)
        self.assertTrue(gh_exists and trivy_exists)

    def test_each_bad_checksum_stops_before_extracting_or_publishing_that_tool(self):
        for tool in ("gh", "trivy"):
            with self.subTest(tool=tool):
                result, paths, gh_exists, trivy_exists = self.install(corrupt=tool)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(paths, [])
                self.assertFalse(gh_exists if tool == "gh" else trivy_exists)

    def test_download_failure_is_not_a_runner_tool_fallback(self):
        result, paths, gh_exists, trivy_exists = self.install(download_error=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(paths, [])
        self.assertFalse(gh_exists or trivy_exists)


if __name__ == "__main__":
    unittest.main()
