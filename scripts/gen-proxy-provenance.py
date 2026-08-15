#!/usr/bin/env python3
"""Generate or verify the vendored SimpleL7Proxy provenance manifest."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _generator import build_parser

ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = ROOT / "proxy"
MANIFEST = PROXY_ROOT / "upstream-provenance.json"
UPSTREAM_REPOSITORY = "https://github.com/microsoft/SimpleL7Proxy.git"
UPSTREAM_COMMIT = "d9eb1d1fa42820792a9699bfc253562fba07d977"
SOURCE_SCOPES = ("Shared", "Shared-parser", "SimpleL7Proxy")

AI4IA_PATCH_REASONS = {
    "Shared-parser/Shared-parser.csproj": (
        "Remove unused runtime packages and retain the logging abstraction used by "
        "the parser."
    ),
    "Shared/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
    "Shared-parser/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
    "Shared-parser/StreamProcessor/JsonStreamProcessor.cs": (
        "Flush streamed response lines immediately instead of buffering tokens."
    ),
    "SimpleL7Proxy/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
    "SimpleL7Proxy/Config/ConfigFactory.cs": (
        "Redact declared secrets and remove warm-reload value logging."
    ),
    "SimpleL7Proxy/Config/ConfigMetadata.cs": (
        "Declare explicit secret metadata for configuration options."
    ),
    "SimpleL7Proxy/Config/AppConfigService.cs": (
        "Keep a failed App Configuration download from dereferencing a null result "
        "and spinning the refresh loop without its normal interval."
    ),
    "SimpleL7Proxy/Config/IncomingAuthValidator.cs": (
        "Honor the configured key header and fail closed for unsigned OAuth modes."
    ),
    "SimpleL7Proxy/Config/ProxyConfig.cs": (
        "Mark inbound authentication keys as secret configuration."
    ),
    "SimpleL7Proxy/Config/SecretComparer.cs": (
        "AI4IA constant-time comparison helper for opaque authentication keys."
    ),
    "SimpleL7Proxy/Async/BlobStorage/BlobWorkerPump.cs": (
        "Document Application Insights 3.x metric emission accurately."
    ),
    "SimpleL7Proxy/Events/RequestFilterTelemetryProcessor.cs": (
        "Migrate duplicate request and HTTP dependency filtering to Application "
        "Insights 3.x OpenTelemetry processor APIs."
    ),
    "SimpleL7Proxy/Events/ProxyEvent.cs": (
        "Migrate event metrics and legacy correlation dimensions to supported "
        "Application Insights 3.x APIs."
    ),
    "SimpleL7Proxy/Program.cs": (
        "Configure Application Insights 3.x sampling and register AI4IA's "
        "OpenTelemetry duplicate-telemetry filter."
    ),
    "SimpleL7Proxy/RequestData.cs": (
        "Derive Azure deployment names from request paths when model is absent."
    ),
    "SimpleL7Proxy/server.cs": (
        "Compare opaque inbound authentication keys exactly and in constant time; remove "
        "redundant request-null control flow; return 404 for privileged legacy diagnostics "
        "before auth or worker dispatch."
    ),
    "SimpleL7Proxy/SimpleL7Proxy.csproj": (
        "Keep runtime dependencies current, remove unsupported Application Insights "
        "2.x packages, and declare the OpenTelemetry processor API."
    ),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonicalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _canonical_sha256(data: bytes) -> str:
    return _sha256(_canonicalize(data))


def _local_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for scope in SOURCE_SCOPES:
        scope_root = PROXY_ROOT / scope
        for path in scope_root.rglob("*"):
            if not path.is_file() or {"bin", "obj"}.intersection(path.parts):
                continue
            relative = path.relative_to(PROXY_ROOT).as_posix()
            files[relative] = path.read_bytes()
    return files


def _upstream_files(ref: str) -> dict[str, bytes]:
    command = [
        "git",
        "ls-tree",
        "-r",
        "--name-only",
        ref,
        "--",
        *(f"src/{scope}" for scope in SOURCE_SCOPES),
    ]
    paths = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {
        path.removeprefix("src/"): subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        for path in paths
    }


def _resolve_commit(ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().lower()


def _validated_upstream_files(ref: str) -> dict[str, bytes]:
    resolved = _resolve_commit(ref)
    if resolved != UPSTREAM_COMMIT:
        raise ValueError(
            f"{ref!r} resolves to {resolved}, expected pinned upstream "
            f"commit {UPSTREAM_COMMIT}"
        )
    return _upstream_files(ref)


def generate(upstream_ref: str) -> dict:
    upstream = _validated_upstream_files(upstream_ref)
    local = _local_files()
    missing = sorted(set(upstream) - set(local))
    if missing:
        raise ValueError(f"upstream files are missing locally: {missing}")

    files: dict[str, dict[str, str]] = {}
    patch_paths: set[str] = set()
    for path in sorted(local):
        local_bytes = local[path]
        entry = {"localCanonicalSha256": _canonical_sha256(local_bytes)}
        if path not in upstream:
            entry["disposition"] = "ai4ia-added"
            patch_paths.add(path)
        else:
            upstream_bytes = upstream[path]
            entry["upstreamRawSha256"] = _sha256(upstream_bytes)
            entry["upstreamCanonicalSha256"] = _canonical_sha256(upstream_bytes)
            if _canonicalize(local_bytes) == _canonicalize(upstream_bytes):
                entry["disposition"] = "upstream-equivalent"
            else:
                entry["disposition"] = "ai4ia-patched"
                patch_paths.add(path)
        files[path] = entry

    declared = set(AI4IA_PATCH_REASONS)
    if patch_paths != declared:
        raise ValueError(
            "AI4IA patch declarations do not match measured drift: "
            f"undeclared={sorted(patch_paths - declared)}, "
            f"stale={sorted(declared - patch_paths)}"
        )

    counts = Counter(entry["disposition"] for entry in files.values())
    return {
        "schemaVersion": 2,
        "upstream": {
            "repository": UPSTREAM_REPOSITORY,
            "commit": UPSTREAM_COMMIT,
            "sourceRoot": "src",
        },
        "sourceScopes": list(SOURCE_SCOPES),
        "counts": dict(sorted(counts.items())),
        "patches": [
            {"path": path, "reason": AI4IA_PATCH_REASONS[path]}
            for path in sorted(AI4IA_PATCH_REASONS)
        ],
        "files": files,
    }


def check() -> list[str]:
    errors: list[str] = []
    try:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read {MANIFEST.relative_to(ROOT)}: {exc}"]

    upstream = document.get("upstream") or {}
    if upstream.get("repository") != UPSTREAM_REPOSITORY:
        errors.append("upstream repository does not match the audited source")
    if upstream.get("commit") != UPSTREAM_COMMIT:
        errors.append("upstream commit does not match the audited pin")
    if tuple(document.get("sourceScopes") or ()) != SOURCE_SCOPES:
        errors.append("source scopes changed without updating the verifier")

    local = _local_files()
    recorded = document.get("files") or {}
    if set(local) != set(recorded):
        errors.append(
            "manifest coverage drift: "
            f"unrecorded={sorted(set(local) - set(recorded))}, "
            f"missing={sorted(set(recorded) - set(local))}"
        )

    measured_counts: Counter[str] = Counter()
    for path in sorted(set(local).intersection(recorded)):
        entry = recorded[path]
        local_bytes = local[path]
        actual_hash = _canonical_sha256(local_bytes)
        if entry.get("localCanonicalSha256") != actual_hash:
            errors.append(f"{path}: local canonical SHA-256 drift")
        disposition = entry.get("disposition")
        measured_counts[disposition] += 1
        if disposition == "upstream-equivalent":
            if entry.get("upstreamCanonicalSha256") != actual_hash:
                errors.append(
                    f"{path}: canonical content does not match upstream hash"
                )
        elif disposition not in {"ai4ia-patched", "ai4ia-added"}:
            errors.append(f"{path}: unknown disposition {disposition!r}")

    patch_entries = document.get("patches") or []
    patch_paths = {entry.get("path") for entry in patch_entries}
    drift_paths = {
        path
        for path, entry in recorded.items()
        if entry.get("disposition") in {"ai4ia-patched", "ai4ia-added"}
    }
    if patch_paths != drift_paths:
        errors.append("explicit patch list does not match recorded source drift")
    if patch_paths != set(AI4IA_PATCH_REASONS):
        errors.append("explicit patch list does not match the reviewed AI4IA patch set")
    for entry in patch_entries:
        path = entry.get("path")
        if entry.get("reason") != AI4IA_PATCH_REASONS.get(path):
            errors.append(f"{path}: patch reason is missing or stale")

    if document.get("counts") != dict(sorted(measured_counts.items())):
        errors.append("manifest counts do not match file dispositions")
    return errors


def main() -> int:
    parser = build_parser(__doc__)
    parser.add_argument("--upstream-ref", default="FETCH_HEAD")
    args = parser.parse_args()
    if args.check:
        errors = check()
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("Proxy provenance manifest is current.")
        return 0

    document = generate(args.upstream_ref)
    MANIFEST.write_text(
        json.dumps(document, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {MANIFEST.relative_to(ROOT)} with {len(document['files'])} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
