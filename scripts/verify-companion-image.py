#!/usr/bin/env python3
"""Verify the optional CompanionApp image's attestations before anything deploys it.

The CompanionApp telemetry console is not an azd service. The manual
``.github/workflows/companion-image.yml`` builds, scans, pushes and attests one
digest. deploy.yml runs this gate before provisioning, because Bicep references
the digest during provisioning. The gate re-verifies the SLSA provenance and SPDX
attestations for exactly that digest with the checksum-pinned GitHub CLI.
The certificate must name the companion workflow on main and a GitHub-hosted
runner, and the signed statement must describe exactly this image.

A disabled console needs no image and exits 0. An enabled console has no skip
mode, and no tag, rebuild or unsigned approval path. Output and errors are
fixed, content-free reasons; no CLI stderr, token or payload is echoed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence

GH_VERSION = "2.100.0"
ISSUER = "https://token.actions.githubusercontent.com"
REF = "refs/heads/main"
WORKFLOW = ".github/workflows/companion-image.yml"
PREDICATES = {
    "provenance": "https://slsa.dev/provenance/v1",
    "sbom": "https://spdx.dev/Document/v2.3",
}
MAX_VERIFY_BYTES = 64 * 1024 * 1024
MAX_ATTESTATIONS = 16
VERIFY_TIMEOUT = 180
IMAGE = re.compile(
    r"(?P<name>(?P<registry>[a-z0-9]{5,50}\.azurecr\.io)/ai4ia/companion-(?P<env>[a-z0-9-]{1,64}))"
    r"@sha256:(?P<digest>[0-9a-f]{64})"
)
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
COMMIT = re.compile(r"[0-9a-f]{40}")
TRUE_VALUES = {"true", "1"}
FALSE_VALUES = {"", "false", "0"}


class CompanionImageError(ValueError):
    """A fixed safe reason, without CLI stderr, credentials or payload excerpts."""


def require(condition: object, reason: str) -> None:
    if not condition:
        raise CompanionImageError(reason)


def enabled(value: str | None) -> bool:
    """azd reads the flag as a Bicep bool; refuse anything this gate cannot mirror."""

    normalized = (value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise CompanionImageError("AI4IA_COMPANION_APP_ENABLED must be true or false")


def parse_image(image: str, registry: str, environment: str) -> tuple[str, str]:
    match = IMAGE.fullmatch(image.strip())
    require(
        match,
        "the CompanionApp image must be <registry>.azurecr.io/ai4ia/companion-<env>@sha256:<digest>",
    )
    assert match is not None
    require(match["registry"] == registry.strip().lower(), "the image is not in this environment's registry")
    require(match["env"] == environment.strip().lower(), "the image is not this environment's companion repository")
    return match["name"], match["digest"]


def strict_json(data: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON field in verification output")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise CompanionImageError("non-JSON constant in verification output")

    try:
        value = json.loads(data, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise CompanionImageError("invalid verification output") from exc
    pending = [(value, 0)]
    nodes = 0
    while pending:
        node, depth = pending.pop()
        nodes += 1
        require(depth <= 48 and nodes <= 1_000_000, "verification output exceeds bounds")
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)
        elif isinstance(node, float):
            require(math.isfinite(node), "non-finite number in verification output")
    return value


def run_bounded(command: list[str], *, limit: int, timeout: float, env: Mapping[str, str] | None = None) -> bytes:
    """Bound stdout while it is produced; never retain raw tool or network stderr."""

    data = bytearray()
    errors: list[OSError] = []
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=None if env is None else dict(env),
    ) as process:
        assert process.stdout is not None

        def drain() -> None:
            try:
                while len(data) <= limit:
                    chunk = process.stdout.read(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        return
                    data.extend(chunk)
                process.kill()
            except OSError as exc:
                errors.append(exc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            reader.join(timeout=5)
            raise CompanionImageError("attestation verifier timed out") from exc
        reader.join(timeout=5)
        require(len(data) <= limit, "attestation verifier output exceeds bounds")
        require(not reader.is_alive() and not errors, "attestation verifier output unavailable")
        require(code == 0, "attestation verification failed; the CompanionApp image is not authorized")
    return bytes(data)


def workflow_identity(repository: str) -> str:
    return f"https://github.com/{repository}/{WORKFLOW}@{REF}"


def verify_command(executable: str, image: str, repository: str, kind: str) -> list[str]:
    return [
        executable, "attestation", "verify", f"oci://{image}",
        "--repo", repository,
        "--cert-identity", workflow_identity(repository),
        "--cert-oidc-issuer", ISSUER, "--deny-self-hosted-runners",
        "--source-ref", REF,
        "--predicate-type", PREDICATES[kind],
        "--limit", str(MAX_ATTESTATIONS),
        "--hostname", "github.com", "--format", "json",
    ]


def validate_verification(
    value: object,
    *,
    name: str,
    digest: str,
    kind: str,
    repository: str,
    expect_commit: str | None = None,
    expect_run: str | None = None,
) -> None:
    """Every returned attestation must describe this image from the companion workflow on main.

    Identical bytes promoted twice carry two valid attestations. A promotion run
    additionally requires one of them to be its own, bound to its commit and run.
    """

    require(isinstance(value, list) and 1 <= len(value) <= MAX_ATTESTATIONS,
            "no bounded set of verified attestations was returned")
    assert isinstance(value, list)
    identity = workflow_identity(repository)
    source = f"https://github.com/{repository}"
    bound = False
    for entry in value:
        require(isinstance(entry, dict), "invalid attestation verification entry")
        result = entry.get("verificationResult")
        require(isinstance(result, dict), "missing cryptographic verification result")
        signature = result.get("signature")
        require(isinstance(signature, dict), "missing verified signature")
        certificate = signature.get("certificate")
        require(isinstance(certificate, dict), "missing verified certificate")
        require(certificate.get("subjectAlternativeName") == identity, "wrong verified workflow identity")
        commit = certificate.get("sourceRepositoryDigest")
        require(isinstance(commit, str) and COMMIT.fullmatch(commit), "missing verified source commit")
        claims = {
            "issuer": ISSUER,
            "sourceRepositoryURI": source,
            "sourceRepositoryRef": REF,
            "buildSignerURI": identity,
            "buildSignerDigest": commit,
            "buildConfigURI": identity,
            "buildConfigDigest": commit,
            "runnerEnvironment": "github-hosted",
        }
        require(all(certificate.get(key) == expected for key, expected in claims.items()),
                "verified certificate is not the companion workflow on main")
        timestamps = result.get("verifiedTimestamps")
        require(isinstance(timestamps, list) and 1 <= len(timestamps) <= 16,
                "missing verified signing timestamp")
        statement = result.get("statement")
        require(isinstance(statement, dict), "missing verified statement")
        assert isinstance(statement, dict)
        require(
            statement.get("_type") == "https://in-toto.io/Statement/v1"
            and statement.get("subject") == [{"name": name, "digest": {"sha256": digest}}]
            and statement.get("predicateType") == PREDICATES[kind],
            "wrong attestation subject or predicate type",
        )
        predicate = statement.get("predicate")
        require(isinstance(predicate, dict), "missing attestation predicate")
        assert isinstance(predicate, dict)
        if kind == "sbom":
            require(predicate.get("spdxVersion") == "SPDX-2.3", "signed SBOM is not SPDX 2.3")
        else:
            definition = predicate.get("buildDefinition")
            require(isinstance(definition, dict), "incomplete SLSA provenance")
            assert isinstance(definition, dict)
            require(
                definition.get("buildType") == "https://actions.github.io/buildtypes/workflow/v1"
                and definition.get("externalParameters") == {
                    "workflow": {"ref": REF, "repository": source, "path": WORKFLOW},
                }
                and definition.get("resolvedDependencies") == [{
                    "uri": f"git+{source}@{REF}", "digest": {"gitCommit": commit},
                }],
                "SLSA provenance does not describe the companion workflow on main",
            )
        if expect_commit is not None or expect_run is not None:
            bound = bound or (
                (expect_commit is None or commit == expect_commit)
                and (expect_run is None or certificate.get("runInvocationURI") == expect_run)
            )
    if expect_commit is not None or expect_run is not None:
        require(bound, "no verified attestation belongs to this promotion run")


def verify(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    if not enabled(args.enabled if args.enabled is not None else env.get("AI4IA_COMPANION_APP_ENABLED")):
        return "CompanionApp is disabled; no image is referenced or verified."
    repository = env.get("GITHUB_REPOSITORY", "")
    require(REPOSITORY.fullmatch(repository), "invalid GitHub repository identity")
    require(args.image, "the CompanionApp is enabled but AI4IA_COMPANION_APP_IMAGE is empty")
    require(args.registry, "the environment registry login server is required")
    require(args.environment, "the azd environment name is required")
    name, digest = parse_image(args.image, args.registry, args.environment)
    if args.expect_source_commit is not None:
        require(COMMIT.fullmatch(args.expect_source_commit), "invalid expected source commit")
    executable = shutil.which("gh")
    require(executable, "the pinned GitHub CLI is not installed")
    assert executable is not None
    version = run_bounded([executable, "--version"], limit=4096, timeout=30, env=env)
    require(version.startswith(f"gh version {GH_VERSION} ".encode()), "unexpected GitHub CLI version")
    image = f"{name}@sha256:{digest}"
    for kind in ("provenance", "sbom"):
        output = run_bounded(
            verify_command(executable, image, repository, kind),
            limit=MAX_VERIFY_BYTES, timeout=VERIFY_TIMEOUT, env=env,
        )
        validate_verification(
            strict_json(output), name=name, digest=digest, kind=kind, repository=repository,
            expect_commit=args.expect_source_commit, expect_run=args.expect_run_invocation,
        )
    return f"Verified SLSA provenance and SPDX attestations from {WORKFLOW} on main for {image}."


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--enabled", help="the AI4IA_COMPANION_APP_ENABLED value (default: environment)")
    parser.add_argument("--image", default="", help="digest-pinned CompanionApp image reference")
    parser.add_argument("--registry", default="", help="this environment's ACR login server")
    parser.add_argument("--environment", default="", help="the azd environment name")
    parser.add_argument("--expect-source-commit", help="promotion only: the commit this run built")
    parser.add_argument("--expect-run-invocation", help="promotion only: this run's invocation URI")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        print(verify(parse_args(sys.argv[1:] if argv is None else argv), os.environ))
    except CompanionImageError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
