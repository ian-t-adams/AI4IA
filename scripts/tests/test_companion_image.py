"""Contracts for the optional CompanionApp image promotion and its pre-provision gate.

The verifier is exercised with synthetic, correctly shaped gh 2.100.0 verification
output and a stubbed CLI, so no network, credential or registry is involved. Every
rejection is paired with an accepted control built from the same fixture.
"""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import yaml

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "scripts" / "verify-companion-image.py"
PROMOTION = ROOT / ".github" / "workflows" / "companion-image.yml"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
AZURE_YAML = ROOT / "azure.yaml"

companion = load_script("verify_companion_image", VERIFIER)

REPOSITORY = "fixture-owner/fixture-repo"
REGISTRY = "crfixture01.azurecr.io"
ENVIRONMENT = "ai4ia-fixture"
NAME = f"{REGISTRY}/ai4ia/companion-{ENVIRONMENT}"
DIGEST = "b" * 64
IMAGE = f"{NAME}@sha256:{DIGEST}"
COMMIT = "c" * 40
RUN = f"https://github.com/{REPOSITORY}/actions/runs/123/attempts/1"
IDENTITY = f"https://github.com/{REPOSITORY}/.github/workflows/companion-image.yml@refs/heads/main"


def attestation(kind: str, *, commit: str = COMMIT, run: str = RUN) -> dict:
    source = f"https://github.com/{REPOSITORY}"
    predicate: dict
    if kind == "sbom":
        predicate = {"spdxVersion": "SPDX-2.3", "name": NAME}
    else:
        predicate = {
            "buildDefinition": {
                "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                "externalParameters": {"workflow": {
                    "ref": "refs/heads/main", "repository": source,
                    "path": ".github/workflows/companion-image.yml",
                }},
                "resolvedDependencies": [{
                    "uri": f"git+{source}@refs/heads/main", "digest": {"gitCommit": commit},
                }],
            },
            "runDetails": {"builder": {"id": IDENTITY}, "metadata": {"invocationId": run}},
        }
    return {"verificationResult": {
        "signature": {"certificate": {
            "subjectAlternativeName": IDENTITY,
            "issuer": "https://token.actions.githubusercontent.com",
            "sourceRepositoryURI": source,
            "sourceRepositoryDigest": commit,
            "sourceRepositoryRef": "refs/heads/main",
            "buildSignerURI": IDENTITY,
            "buildSignerDigest": commit,
            "buildConfigURI": IDENTITY,
            "buildConfigDigest": commit,
            "runnerEnvironment": "github-hosted",
            "runInvocationURI": run,
        }},
        "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2026-09-26T00:00:00Z"}],
        "statement": {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [{"name": NAME, "digest": {"sha256": DIGEST}}],
            "predicateType": companion.PREDICATES[kind],
            "predicate": predicate,
        },
    }}


def validate(entries: list[dict], kind: str, **expected: str) -> None:
    companion.validate_verification(
        copy.deepcopy(entries), name=NAME, digest=DIGEST, kind=kind, repository=REPOSITORY, **expected,
    )


class VerificationContractTests(unittest.TestCase):
    def test_a_correct_attestation_is_accepted_for_both_predicates(self) -> None:
        for kind in ("provenance", "sbom"):
            with self.subTest(kind=kind):
                validate([attestation(kind)], kind)
                validate([attestation(kind)], kind, expect_commit=COMMIT, expect_run=RUN)

    def test_identity_subject_and_runner_mismatches_are_rejected(self) -> None:
        def mutate(kind: str, change) -> list[dict]:
            entry = attestation(kind)
            change(entry["verificationResult"])
            return [entry]

        cases = {
            "deploy workflow identity": lambda r: r["signature"]["certificate"].update(
                subjectAlternativeName=IDENTITY.replace("companion-image.yml", "deploy.yml")),
            "non-main ref": lambda r: r["signature"]["certificate"].update(sourceRepositoryRef="refs/heads/feature"),
            "self-hosted runner": lambda r: r["signature"]["certificate"].update(runnerEnvironment="self-hosted"),
            "other repository": lambda r: r["signature"]["certificate"].update(
                sourceRepositoryURI="https://github.com/other/repo"),
            "other image digest": lambda r: r["statement"].update(
                subject=[{"name": NAME, "digest": {"sha256": "d" * 64}}]),
            "other image name": lambda r: r["statement"].update(
                subject=[{"name": NAME + "-x", "digest": {"sha256": DIGEST}}]),
            "two subjects": lambda r: r["statement"]["subject"].append(
                {"name": NAME, "digest": {"sha256": "d" * 64}}),
            "wrong predicate": lambda r: r["statement"].update(predicateType="https://example.test/other"),
            "no timestamp": lambda r: r.update(verifiedTimestamps=[]),
            "signer differs from source": lambda r: r["signature"]["certificate"].update(buildSignerDigest="e" * 40),
        }
        for kind in ("provenance", "sbom"):
            for label, change in cases.items():
                with self.subTest(kind=kind, case=label):
                    with self.assertRaises(companion.CompanionImageError):
                        validate(mutate(kind, change), kind)
        # Control: the unmutated fixture is accepted for both predicates.
        for kind in ("provenance", "sbom"):
            validate([attestation(kind)], kind)

    def test_one_bad_attestation_among_good_ones_is_rejected(self) -> None:
        good = attestation("provenance")
        bad = attestation("provenance")
        bad["verificationResult"]["signature"]["certificate"]["runnerEnvironment"] = "self-hosted"
        with self.assertRaises(companion.CompanionImageError):
            validate([good, bad], "provenance")
        validate([good, attestation("provenance", commit="f" * 40)], "provenance")

    def test_promotion_requires_an_attestation_from_this_exact_run(self) -> None:
        older = attestation("provenance", commit="f" * 40, run=RUN.replace("/123/", "/99/"))
        with self.assertRaisesRegex(companion.CompanionImageError, "belongs to this promotion run"):
            validate([older], "provenance", expect_commit=COMMIT, expect_run=RUN)
        # Control: the same image re-promoted keeps its older attestation and adds this run's.
        validate([older, attestation("provenance")], "provenance", expect_commit=COMMIT, expect_run=RUN)
        # Deploy-time verification of an existing digest accepts any valid promotion of it.
        validate([older], "provenance")

    def test_slsa_provenance_must_name_the_companion_workflow(self) -> None:
        entry = attestation("provenance")
        definition = entry["verificationResult"]["statement"]["predicate"]["buildDefinition"]
        definition["externalParameters"]["workflow"]["path"] = ".github/workflows/deploy.yml"
        with self.assertRaisesRegex(companion.CompanionImageError, "SLSA provenance"):
            validate([entry], "provenance")

    def test_bounds_on_the_attestation_set(self) -> None:
        with self.assertRaises(companion.CompanionImageError):
            validate([], "sbom")
        with self.assertRaises(companion.CompanionImageError):
            validate([attestation("sbom")] * (companion.MAX_ATTESTATIONS + 1), "sbom")
        validate([attestation("sbom")] * companion.MAX_ATTESTATIONS, "sbom")


class ImageReferenceTests(unittest.TestCase):
    def test_only_this_environments_digest_reference_is_accepted(self) -> None:
        self.assertEqual(companion.parse_image(IMAGE, REGISTRY, ENVIRONMENT), (NAME, DIGEST))
        for image, registry, environment in (
            (f"{NAME}:latest", REGISTRY, ENVIRONMENT),
            (f"{NAME}:latest@sha256:{DIGEST}", REGISTRY, ENVIRONMENT),
            (f"docker.io/ai4ia/companion-{ENVIRONMENT}@sha256:{DIGEST}", REGISTRY, ENVIRONMENT),
            (IMAGE, "crother01.azurecr.io", ENVIRONMENT),
            (IMAGE, REGISTRY, "ai4ia-other"),
            (f"{REGISTRY}/ai4ia/web-{ENVIRONMENT}@sha256:{DIGEST}", REGISTRY, ENVIRONMENT),
            (f"{NAME}@sha256:{DIGEST[:-1]}", REGISTRY, ENVIRONMENT),
        ):
            with self.subTest(image=image, registry=registry, environment=environment):
                with self.assertRaises(companion.CompanionImageError):
                    companion.parse_image(image, registry, environment)

    def test_flag_values_match_what_bicep_accepts(self) -> None:
        for value in ("true", "True", " TRUE ", "1"):
            self.assertTrue(companion.enabled(value))
        for value in ("", None, "false", "0", "False"):
            self.assertFalse(companion.enabled(value))
        with self.assertRaises(companion.CompanionImageError):
            companion.enabled("yes")


class GateExecutionTests(unittest.TestCase):
    """Drive verify() end to end with the pinned CLI stubbed at the process seam."""

    ENV = {"GITHUB_REPOSITORY": REPOSITORY}

    def run_gate(self, outputs: dict[str, list[dict]], *args: str) -> tuple[str, list[list[str]]]:
        calls: list[list[str]] = []

        def fake(command: list[str], **_kwargs) -> bytes:
            calls.append(command)
            if command[1] == "--version":
                return b"gh version 2.100.0 (2026-09-01)\n"
            kind = "sbom" if companion.PREDICATES["sbom"] in command else "provenance"
            return json.dumps(outputs[kind]).encode()

        namespace = companion.parse_args([
            "--enabled", "true", "--image", IMAGE, "--registry", REGISTRY, "--environment", ENVIRONMENT, *args,
        ])
        with (
            mock.patch.object(companion.shutil, "which", return_value="/tools/gh"),
            mock.patch.object(companion, "run_bounded", side_effect=fake),
        ):
            return companion.verify(namespace, self.ENV), calls

    def test_disabled_console_verifies_nothing(self) -> None:
        with mock.patch.object(companion, "run_bounded") as run:
            message = companion.verify(companion.parse_args(["--enabled", "false"]), self.ENV)
        self.assertIn("disabled", message)
        run.assert_not_called()

    def test_enabled_console_without_an_image_fails_closed(self) -> None:
        with self.assertRaisesRegex(companion.CompanionImageError, "IMAGE is empty"):
            companion.verify(companion.parse_args(["--enabled", "true"]), self.ENV)

    def test_both_predicates_are_verified_against_the_exact_digest(self) -> None:
        good = {kind: [attestation(kind)] for kind in ("provenance", "sbom")}
        message, calls = self.run_gate(good)
        self.assertIn(IMAGE, message)
        verifications = [call for call in calls if call[1] == "attestation"]
        self.assertEqual(len(verifications), 2)
        for call in verifications:
            self.assertEqual(call[3], f"oci://{IMAGE}")
            for flag, value in (
                ("--repo", REPOSITORY), ("--cert-identity", IDENTITY),
                ("--source-ref", "refs/heads/main"),
                ("--cert-oidc-issuer", "https://token.actions.githubusercontent.com"),
            ):
                self.assertEqual(call[call.index(flag) + 1], value)
            self.assertIn("--deny-self-hosted-runners", call)
        # Control: a bad SBOM attestation alone stops the same gate.
        bad = {**good, "sbom": [attestation("sbom")]}
        bad["sbom"][0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = "d" * 64
        with self.assertRaises(companion.CompanionImageError):
            self.run_gate(bad)

    def test_an_unpinned_cli_is_refused_before_any_verification(self) -> None:
        def fake(command: list[str], **_kwargs) -> bytes:
            return b"gh version 2.99.0 (2026-08-01)\n"

        namespace = companion.parse_args([
            "--enabled", "true", "--image", IMAGE, "--registry", REGISTRY, "--environment", ENVIRONMENT,
        ])
        with (
            mock.patch.object(companion.shutil, "which", return_value="/tools/gh"),
            mock.patch.object(companion, "run_bounded", side_effect=fake) as run,
        ):
            with self.assertRaisesRegex(companion.CompanionImageError, "unexpected GitHub CLI version"):
                companion.verify(namespace, self.ENV)
        self.assertEqual(run.call_count, 1)


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.promotion = yaml.safe_load(PROMOTION.read_text(encoding="utf-8"))
        self.deploy = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))

    def test_promotion_is_manual_main_only_and_serialized_with_deploy(self) -> None:
        triggers = self.promotion.get("on", self.promotion.get(True))
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        self.assertFalse(triggers["workflow_dispatch"], "promotion takes no caller inputs")
        self.assertEqual(self.promotion["permissions"], {})
        self.assertEqual(self.promotion["concurrency"], self.deploy["concurrency"])
        (job,) = self.promotion["jobs"].values()
        self.assertEqual(job["if"], "${{ github.ref == 'refs/heads/main' }}")
        self.assertEqual(job["environment"], "production")
        self.assertEqual(job["permissions"], {"contents": "read", "id-token": "write", "attestations": "write"})

    def test_promotion_builds_once_scans_attests_and_verifies_in_order(self) -> None:
        (job,) = self.promotion["jobs"].values()
        steps = job["steps"]
        names = [step["name"] for step in steps]
        order = [
            "Build and push the CompanionApp image, recording its digest",
            "Scan the pushed image for HIGH/CRITICAL vulnerabilities",
            "Generate the CompanionApp SPDX SBOM",
            "Attest CompanionApp build provenance",
            "Attest CompanionApp SPDX SBOM",
            "Verify this run's CompanionApp attestations",
            "Record the promoted digest",
        ]
        self.assertEqual([name for name in names if name in order], order)
        build = steps[names.index(order[0])]["run"]
        self.assertEqual(build.count("docker build"), 1)
        self.assertIn("--file proxy/CompanionApp.Dockerfile", build)
        self.assertNotIn("azd", build)
        scan = steps[names.index(order[1])]["run"]
        for flag in ("--severity HIGH,CRITICAL", "--exit-code 1", "--ignorefile proxy/.trivyignore"):
            self.assertIn(flag, scan)
        for name in order[3:5]:
            inputs = steps[names.index(name)]["with"]
            self.assertIs(inputs["push-to-registry"], True)
            self.assertIs(inputs["create-storage-record"], False)
            self.assertEqual(inputs["subject-digest"], "${{ steps.image.outputs.digest }}")
        verify = steps[names.index(order[5])]["run"]
        self.assertIn("scripts/verify-companion-image.py", verify)
        self.assertIn("--expect-source-commit", verify)
        self.assertIn("--expect-run-invocation", verify)
        # Every step blocks: a failed scan, attestation or verification stops promotion.
        self.assertNotIn("continue-on-error", job)
        for step in steps:
            self.assertNotIn("continue-on-error", step, step["name"])
            self.assertNotIn("|| true", step.get("run", ""), step["name"])
        for step in steps:
            self.assertNotIn("azd deploy", step.get("run", ""))
            self.assertNotIn("containerapp update", step.get("run", ""))

    def test_promotion_reuses_deploy_tool_checksums(self) -> None:
        def tools(document: dict, job: str) -> dict:
            step = next(s for s in document["jobs"][job]["steps"] if s["name"] == "Install pinned image evidence tools")
            return step["env"]

        (job_name,) = self.promotion["jobs"]
        self.assertEqual(tools(self.promotion, job_name), tools(self.deploy, "deploy"))

    def test_deploy_verifies_the_companion_digest_before_capture_and_provision(self) -> None:
        steps = self.deploy["jobs"]["deploy"]["steps"]
        names = [step.get("name") for step in steps]
        gate = names.index("Verify the CompanionApp image attestations before provisioning")
        for later in ("Capture pre-provision revisions (rollback target)", "Provision infrastructure"):
            self.assertLess(gate, names.index(later))
        for earlier in ("Install pinned image evidence tools", "Log in to Azure CLI (OIDC)"):
            self.assertGreater(gate, names.index(earlier))
        step = steps[gate]
        self.assertEqual(step["if"], "${{ github.event_name != 'workflow_dispatch' || inputs.provision }}")
        # Blocking: a failed verification must stop the job before provisioning.
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("continue-on-error", self.deploy["jobs"]["deploy"])
        self.assertNotIn("|| true", step["run"])
        self.assertIn("set -euo pipefail", step["run"])
        self.assertIn("scripts/verify-companion-image.py", step["run"])
        self.assertIn('--image "${AI4IA_COMPANION_APP_IMAGE:-}"', step["run"])
        self.assertEqual(step["env"], {"GH_TOKEN": "${{ github.token }}"})
        env = self.deploy["jobs"]["deploy"]["env"]
        for name in (
            "AI4IA_COMPANION_APP_ENABLED", "AI4IA_COMPANION_APP_IMAGE", "AI4IA_COMPANION_APP_ENTRA_CLIENT_ID",
            "AI4IA_COMPANION_APP_ADMIN_GROUP_IDS", "AI4IA_COMPANION_APP_ADMIN_PRINCIPAL_IDS",
            "AI4IA_COMPANION_APP_ALLOWED_IP_RANGES", "AI4IA_COMPANION_APP_MIN_REPLICAS",
        ):
            self.assertEqual(env[name], "${{ vars." + name + " }}")

    def test_the_three_azd_services_are_unchanged(self) -> None:
        services = yaml.safe_load(AZURE_YAML.read_text(encoding="utf-8"))["services"]
        self.assertEqual(set(services), {"web", "api", "proxy"})
        deploy = next(s for s in self.deploy["jobs"]["deploy"]["steps"] if s.get("id") == "deploy")
        self.assertEqual(len(re.findall(r"\bazd deploy \w+ --from-package", deploy["run"])), 3)
        self.assertNotIn("companion", deploy["run"])


if __name__ == "__main__":
    unittest.main()
