#!/usr/bin/env python3
"""Bind this release's SPDX and signed provenance to every image before deployment.

Cryptography is performed by the checksum-pinned GitHub CLI, never by accepting
caller-supplied "verified" JSON. Its certificate fields establish identity; its
signed statement must additionally describe exactly one expected image and the
expected predicate. No Azure writes, model calls, fallback or skip mode exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from _image_refs import SERVICES, ImageInputError, parse_expected_images

GH_VERSION = "2.100.0"
TRIVY_VERSION = "0.71.2"
PROJECT = "ai4ia"
REF = "refs/heads/main"
WORKFLOW = ".github/workflows/deploy.yml"
ISSUER = "https://token.actions.githubusercontent.com"
PREDICATES = {
    "provenance": "https://slsa.dev/provenance/v1",
    "sbom": "https://spdx.dev/Document/v2.3",
}
MAX_SBOM_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 24 * 1024 * 1024
MAX_VERIFY_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
VERIFY_TIMEOUT = 120
HEX256 = re.compile(r"[0-9a-f]{64}\Z")


class ProvenanceError(ValueError):
    """A fixed safe reason, without CLI stderr, credentials or payload excerpts."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProvenanceError(reason)


def strict_json(data: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ProvenanceError("non-JSON constant")

    try:
        value = json.loads(data, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise ProvenanceError("invalid JSON evidence") from exc
    pending = [(value, 0)]
    nodes = 0
    while pending:
        node, depth = pending.pop()
        nodes += 1
        require(depth <= 48 and nodes <= 1_000_000, "JSON structure exceeds bounds")
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)
        elif isinstance(node, float):
            require(math.isfinite(node), "non-finite JSON number")
    return value


def read_bytes(path: Path, limit: int) -> bytes:
    require(path.is_file() and not path.is_symlink(), "missing or non-regular evidence file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(0 < len(data) <= limit, "evidence file exceeds bounds or is empty")
    return data


def read_object(path: Path, limit: int) -> dict:
    value = strict_json(read_bytes(path, limit))
    require(isinstance(value, dict), "JSON object required")
    return value


def encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def identity(env: Mapping[str, str]) -> dict[str, str]:
    repository = env.get("GITHUB_REPOSITORY", "")
    sha = env.get("GITHUB_SHA", "")
    run = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    event = env.get("GITHUB_EVENT_NAME", "")
    require(
        bool(re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", repository))
        and bool(re.fullmatch(r"[0-9a-f]{40}", sha))
        and bool(re.fullmatch(r"[1-9][0-9]{0,19}", run))
        and bool(re.fullmatch(r"[1-9][0-9]{0,5}", attempt)),
        "invalid GitHub release identity",
    )
    workflow_ref = f"{repository}/{WORKFLOW}@{REF}"
    require(
        env.get("GITHUB_SERVER_URL") == "https://github.com"
        and env.get("GITHUB_REF") == REF
        and env.get("GITHUB_WORKFLOW_REF") == workflow_ref
        and env.get("GITHUB_WORKFLOW_SHA") == sha
        and event in {"push", "workflow_dispatch"},
        "only the current main deployment workflow may attest release images",
    )
    return {
        "repository": repository,
        "sourceCommit": sha,
        "sourceRef": REF,
        "workflowIdentity": f"https://github.com/{workflow_ref}",
        "runInvocation": f"https://github.com/{repository}/actions/runs/{run}/attempts/{attempt}",
        "event": event,
    }


def release_images(values: Sequence[str], environment: str) -> dict[str, str]:
    try:
        images = parse_expected_images(values)
    except ImageInputError as exc:
        raise ProvenanceError("invalid image arguments") from exc
    require(
        len(values) == len(SERVICES) and set(images) == set(SERVICES)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,60}", environment)),
        "exactly one image per release service and a valid environment are required",
    )
    registries = set()
    for service, reference in images.items():
        require(f"{service}={reference}" in values, "image arguments must not be normalized")
        name, separator, digest = reference.partition("@sha256:")
        registry, slash, repository = name.partition("/")
        labels = registry.split(".")
        require(
            bool(separator) and bool(HEX256.fullmatch(digest)) and bool(slash)
            and len(labels) == 3 and labels[1:] == ["azurecr", "io"]
            and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]", labels[0]))
            and repository == f"{PROJECT}/{service}-{environment.lower()}",
            "expected an exact ACR service repository and SHA-256 digest",
        )
        registries.add(registry)
    require(len(registries) == 1, "release images must share the discovered registry")
    return images


def subjects(args: argparse.Namespace, env: Mapping[str, str]) -> dict:
    return {
        "schemaVersion": 1,
        "identity": identity(env),
        "images": release_images(args.expect_image, env.get("AZURE_ENV_NAME", "")),
    }


def bound_subjects(args: argparse.Namespace, env: Mapping[str, str]) -> dict:
    expected = subjects(args, env)
    require(
        encode(read_object(args.evidence_dir / "subjects.json", MAX_METADATA_BYTES)) == encode(expected),
        "release identity or original image outputs changed",
    )
    return expected


def validate_sbom(document: dict, image: str) -> None:
    require(
        document.get("spdxVersion") == "SPDX-2.3"
        and document.get("SPDXID") == "SPDXRef-DOCUMENT"
        and document.get("dataLicense") == "CC0-1.0"
        and document.get("name") == image,
        "SPDX document must describe the exact image",
    )
    creation = document.get("creationInfo")
    require(
        isinstance(creation, dict)
        and isinstance(creation.get("creators"), list)
        and f"Tool: trivy-{TRIVY_VERSION}" in creation["creators"],
        "SPDX must identify the pinned scanner",
    )
    packages = document.get("packages")
    relationships = document.get("relationships")
    require(
        isinstance(packages, list) and 2 <= len(packages) <= 50_000
        and isinstance(relationships, list) and 1 <= len(relationships) <= 100_000,
        "SPDX package inventory or relationships are missing",
    )
    by_id: dict[str, dict] = {}
    for package in packages:
        require(
            isinstance(package, dict) and isinstance(package.get("SPDXID"), str)
            and isinstance(package.get("name"), str) and bool(package["name"]),
            "invalid SPDX package",
        )
        key = package["SPDXID"]
        require(key.startswith("SPDXRef-") and key not in by_id, "invalid or duplicate SPDX package ID")
        by_id[key] = package
    roots = [key for key, package in by_id.items()
             if package["name"] == image and package.get("primaryPackagePurpose") == "CONTAINER"]
    require(len(roots) == 1, "SPDX must identify one image root")
    children: dict[str, set[str]] = {}
    described = []
    for relation in relationships:
        require(
            isinstance(relation, dict)
            and all(isinstance(relation.get(key), str) for key in (
                "spdxElementId", "relatedSpdxElement", "relationshipType"
            )),
            "invalid SPDX relationship",
        )
        source, target = relation["spdxElementId"], relation["relatedSpdxElement"]
        if source == "SPDXRef-DOCUMENT" and relation["relationshipType"] == "DESCRIBES":
            described.append(target)
        if relation["relationshipType"] in {"CONTAINS", "DEPENDS_ON"}:
            children.setdefault(source, set()).add(target)
    require(described == roots, "SPDX document must describe only its image root")
    pending, reachable = [roots[0]], set()
    while pending:
        key = pending.pop()
        if key not in reachable:
            reachable.add(key)
            pending.extend(children.get(key, ()))
    require(
        any(package.get("primaryPackagePurpose") == "LIBRARY"
            and isinstance(package.get("versionInfo"), str) and package["versionInfo"]
            for key, package in by_id.items() if key in reachable),
        "SPDX has no reachable versioned dependency",
    )


def sboms(directory: Path, images: dict[str, str]) -> dict[str, dict]:
    result = {}
    for service, image in images.items():
        document = read_object(directory / f"{service}.spdx.json", MAX_SBOM_BYTES)
        validate_sbom(document, image)
        result[service] = document
    return result


def run_bounded(command: list[str], *, limit: int, timeout: float) -> bytes:
    """Bound stdout while it is produced; never retain raw tool/network stderr."""
    data = bytearray()
    errors: list[OSError] = []
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
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
            raise ProvenanceError("attestation verifier timed out") from exc
        reader.join(timeout=5)
        require(len(data) <= limit, "attestation verifier output exceeds bounds")
        require(not reader.is_alive() and not errors, "attestation verifier output unavailable")
        require(code == 0, "attestation verifier failed; no image is authorized")
    return bytes(data)


def verify_command(executable: str, image: str, bundle: Path, kind: str, expected: dict) -> list[str]:
    return [
        executable, "attestation", "verify", f"oci://{image}",
        "--bundle", str(bundle), "--repo", expected["repository"],
        "--cert-identity", expected["workflowIdentity"],
        "--cert-oidc-issuer", ISSUER, "--deny-self-hosted-runners",
        "--source-ref", REF, "--source-digest", expected["sourceCommit"],
        "--signer-digest", expected["sourceCommit"],
        "--predicate-type", PREDICATES[kind], "--hostname", "github.com", "--format", "json",
    ]


def validate_verification(value: object, image: str, kind: str, expected: dict, sbom: dict) -> None:
    require(isinstance(value, list) and len(value) == 1, "exactly one verified attestation is required")
    entry = value[0]
    require(isinstance(entry, dict), "invalid attestation verification entry")
    result = entry.get("verificationResult")
    require(isinstance(result, dict), "missing cryptographic verification result")
    signature = result.get("signature")
    require(isinstance(signature, dict), "missing verified signature")
    certificate = signature.get("certificate")
    require(isinstance(certificate, dict), "missing verified certificate")
    require(certificate.get("subjectAlternativeName") == expected["workflowIdentity"],
            "wrong verified workflow identity")
    extensions = certificate.get("extensions")
    require(isinstance(extensions, dict), "missing verified certificate extensions")
    claims = {
        "issuer": ISSUER,
        "sourceRepositoryURI": f"https://github.com/{expected['repository']}",
        "sourceRepositoryDigest": expected["sourceCommit"],
        "sourceRepositoryRef": REF,
        "buildSignerURI": expected["workflowIdentity"],
        "buildSignerDigest": expected["sourceCommit"],
        "buildConfigURI": expected["workflowIdentity"],
        "buildConfigDigest": expected["sourceCommit"],
        "runnerEnvironment": "github-hosted",
        "runInvocationURI": expected["runInvocation"],
        "buildTrigger": expected["event"],
    }
    require(all(extensions.get(key) == value for key, value in claims.items()),
            "verified certificate does not belong to this release")
    timestamps = result.get("verifiedTimestamps")
    require(isinstance(timestamps, list) and 1 <= len(timestamps) <= 16,
            "missing verified signing timestamp")
    for timestamp in timestamps:
        require(isinstance(timestamp, dict) and isinstance(timestamp.get("timestamp"), str),
                "invalid verified signing timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp["timestamp"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProvenanceError("invalid verified signing timestamp") from exc
        require(parsed.tzinfo is not None, "verified signing timestamp must have an offset")
    statement = result.get("statement")
    require(isinstance(statement, dict), "missing verified statement")
    name, digest = image.split("@sha256:")
    require(
        statement.get("_type") == "https://in-toto.io/Statement/v1"
        and statement.get("subject") == [{"name": name, "digest": {"sha256": digest}}]
        and statement.get("predicateType") == PREDICATES[kind],
        "wrong attestation subject or predicate type",
    )
    predicate = statement.get("predicate")
    require(isinstance(predicate, dict), "missing attestation predicate")
    if kind == "sbom":
        require(predicate == sbom, "signed SPDX differs from the generated image SBOM")
        return
    definition, details = predicate.get("buildDefinition"), predicate.get("runDetails")
    require(isinstance(definition, dict) and isinstance(details, dict),
            "incomplete SLSA provenance")
    require(
        definition.get("buildType") == "https://actions.github.io/buildtypes/workflow/v1"
        and definition.get("externalParameters") == {"workflow": {
            "ref": REF, "repository": f"https://github.com/{expected['repository']}", "path": WORKFLOW,
        }}
        and definition.get("resolvedDependencies") == [{
            "uri": f"git+https://github.com/{expected['repository']}@{REF}",
            "digest": {"gitCommit": expected["sourceCommit"]},
        }]
        and details.get("builder") == {"id": expected["workflowIdentity"]}
        and details.get("metadata") == {"invocationId": expected["runInvocation"]},
        "SLSA provenance does not describe this workflow, commit and run",
    )


def evidence_names() -> list[str]:
    return ["subjects.json", *[
        name for service in SERVICES for name in (
            f"{service}.spdx.json",
            *[f"{service}.{kind}.{suffix}.json" for kind in PREDICATES for suffix in ("sigstore", "verification")],
        )
    ]]


def file_hashes(directory: Path) -> dict[str, str]:
    return {name: hashlib.sha256(read_bytes(directory / name, MAX_VERIFY_BYTES)).hexdigest()
            for name in evidence_names()}


def github_output(env: Mapping[str, str], values: dict[str, str]) -> None:
    require(bool(env.get("GITHUB_OUTPUT")), "missing GitHub step output file")
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))


def execute(args: argparse.Namespace, env: Mapping[str, str]) -> None:
    directory = args.evidence_dir
    if args.command == "prepare":
        manifest = subjects(args, env)
        directory.mkdir()
        write_new(directory / "subjects.json", encode(manifest))
        outputs = {}
        for service, image in manifest["images"].items():
            name, digest = image.split("@")
            outputs[f"{service}_name"] = name
            outputs[f"{service}_digest"] = digest
        github_output(env, outputs)
        return
    manifest = bound_subjects(args, env)
    if args.command == "authorize":
        data = read_bytes(directory / "verified-images.json", MAX_METADATA_BYTES)
        require(bool(HEX256.fullmatch(args.proof_sha256 or ""))
                and hashlib.sha256(data).hexdigest() == args.proof_sha256,
                "missing or changed verification proof")
        require(strict_json(data) == {**manifest, "status": "verified", "files": file_hashes(directory)},
                "image outputs or retained evidence changed after verification")
        return
    documents = sboms(directory, manifest["images"])
    if args.command == "sboms":
        return
    require(bool(env.get("GH_TOKEN")), "authenticated attestation verification is required")
    executable = shutil.which("gh")
    require(executable is not None, "pinned GitHub CLI is unavailable")
    version = run_bounded([executable, "--version"], limit=4096, timeout=10)
    require(version.startswith(f"gh version {GH_VERSION} ".encode()), "unexpected GitHub CLI version")
    for service in SERVICES:
        for kind in PREDICATES:
            bundle_source = env.get(f"{service.upper()}_{kind.upper()}_BUNDLE", "")
            require(bool(bundle_source), "missing current-run attestation bundle")
            source = Path(bundle_source)
            runner_temp = Path(env["RUNNER_TEMP"]).resolve()
            require(source.is_absolute() and source.resolve().is_relative_to(runner_temp),
                    "attestation bundle is not a current runner artifact")
            data = read_bytes(source, MAX_BUNDLE_BYTES)
            require(isinstance(strict_json(data), dict), "invalid attestation bundle")
            bundle = directory / f"{service}.{kind}.sigstore.json"
            write_new(bundle, data)
            try:
                output = run_bounded(
                    verify_command(executable, manifest["images"][service], bundle, kind, manifest["identity"]),
                    limit=MAX_VERIFY_BYTES, timeout=VERIFY_TIMEOUT,
                )
                validate_verification(strict_json(output), manifest["images"][service], kind,
                                      manifest["identity"], documents[service])
            except ProvenanceError as exc:
                raise ProvenanceError(f"{service}/{kind}: {exc}") from exc
            write_new(directory / f"{service}.{kind}.verification.json", output)
    proof = encode({**manifest, "status": "verified", "files": file_hashes(directory)})
    require(len(proof) <= MAX_METADATA_BYTES, "verification proof exceeds bounds")
    write_new(directory / "verified-images.json", proof)
    require(bool(env.get("GITHUB_STEP_SUMMARY")), "missing GitHub step summary file")
    with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            "\n### Verified production image proofs\n\n"
            f"Source: `{manifest['identity']['sourceCommit']}`. "
            f"Signing run: {manifest['identity']['runInvocation']}.\n\n"
            "| Service | Exact image | Verified predicates |\n| --- | --- | --- |\n"
            + "".join(f"| {service} | `{image}` | SLSA v1 + SPDX 2.3 |\n"
                      for service, image in manifest["images"].items())
            + "\nThe deployment step rechecks the proof hash and original image outputs before dispatch.\n"
        )
    github_output(env, {"proof_sha256": hashlib.sha256(proof).hexdigest()})
    print("Verified current-run provenance and SPDX for every release image.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "sboms", "verify", "authorize"))
    parser.add_argument("--expect-image", action="append", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--proof-sha256")
    args = parser.parse_args()
    try:
        execute(args, os.environ)
    except ProvenanceError as exc:
        print(f"::error::Production image provenance: {exc}", file=sys.stderr)
        return 1
    except (OSError, KeyError):
        # Do not retain tool stderr, arbitrary paths or environment contents.
        print("::error::Production image provenance: required evidence or tool is unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
