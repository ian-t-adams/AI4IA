#!/usr/bin/env python3
"""Generate or verify the vendored SimpleL7Proxy provenance manifest."""

from __future__ import annotations

import fnmatch
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
UPSTREAM_COMMIT = "b0066b0e53f89abb5e84cfeacda2fdcaca8b081e"
SOURCE_SCOPES = ("Shared", "Shared-parser", "SimpleL7Proxy", "CompanionApp")

AI4IA_PATCH_REASONS = {
    "Shared-parser/Shared-parser.csproj": (
        "Remove unused runtime packages and update "
        "Microsoft.Extensions.Logging.Abstractions to 10.0.12 for the parser."
    ),
    "Shared/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
    "Shared-parser/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
    "Shared-parser/StreamProcessor/JsonStreamProcessor.cs": (
        "Flush streamed response lines immediately instead of buffering tokens."
    ),
    "SimpleL7Proxy/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
    "SimpleL7Proxy/Config/AppConfigKeyPolicy.cs": (
        "AI4IA default-deny App Configuration key policy: only Warm:Sentinel and the "
        "reviewed operational keys may be applied."
    ),
    "SimpleL7Proxy/Config/AppConfigService.cs": (
        "Apply AI4IA's default-deny key policy before any downloaded key is resolved, "
        "log refused key names without values, and accept a test client and control "
        "policy through an internal constructor."
    ),
    "SimpleL7Proxy/Config/ConfigFactory.cs": (
        "Redact declared secrets and remove warm-reload value logging."
    ),
    "SimpleL7Proxy/Config/ConfigMetadata.cs": (
        "Declare explicit secret metadata for configuration options."
    ),
    "SimpleL7Proxy/Config/IncomingAuthValidator.cs": (
        "Trim and default the configured key header over upstream's raw assignment "
        "and fail closed for unsigned OAuth modes."
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
        "Derive Azure deployment names from request paths when model is absent; "
        "retain authenticated one-attempt state across the in-memory worker lifetime."
    ),
    "SimpleL7Proxy/server.cs": (
        "Compare opaque inbound authentication keys exactly and in constant time; remove "
        "redundant request-null control flow; return 404 for privileged legacy diagnostics "
        "before auth or worker dispatch; bind reduction-only attempt metadata only "
        "after successful key authentication."
    ),
    "SimpleL7Proxy/Proxy/NoReplayAttempt.cs": (
        "AI4IA authenticated one-attempt binding, exact byte/model/path HMAC, "
        "mandatory versioned route and non-stripping scoped-host membership, "
        "pre-send claim, unsupported-shape and upstream caller-control refusal, "
        "and nonredirecting HTTP/1.1 transport."
    ),
    "SimpleL7Proxy/Proxy/ProxyWorker.cs": (
        "Fence bounded sends, host fallback, requeue and recovery, including the "
        "iterator's all-open-circuit requeue; retain one-use HTTP clients through "
        "streaming; construct portable diagnostic URIs from the same destination "
        "builder as the transport."
    ),
    "SimpleL7Proxy/Proxy/ProxyData.cs": (
        "Dispose the bounded request's dedicated transport after its response body."
    ),
    "SimpleL7Proxy/Proxy/ProxyHelperUtils.cs": (
        "Redact internal one-attempt metadata in the shared header logger."
    ),
    "SimpleL7Proxy/Proxy/RequeueDelayWorker.cs": (
        "Reject bounded requests before enqueueing or resetting attempt counters."
    ),
    "SimpleL7Proxy/DTO/RequestDataDtoV1.cs": (
        "Refuse bounded request persistence and reject recovered attempt metadata "
        "or versioned paths even when all attempt fields are absent."
    ),
    "SimpleL7Proxy/SimpleL7Proxy.csproj": (
        "Keep runtime dependencies current, including IdentityModel 8.23.0 and "
        "OpenTelemetry 1.19.1; remove unsupported Application Insights 2.x packages "
        "and declare the OpenTelemetry processor API."
    ),
    "CompanionApp/CompanionApp.csproj": (
        "Drop the embedded resources and content items of the excluded deployment "
        "page and chat/vision presets, reference the Blazor script asset package "
        "explicitly so locked restore does not depend on restore layering or SDK patch, "
        "and keep runtime dependencies current."
    ),
    "CompanionApp/Program.cs": (
        "Hosted mode: a first-in-pipeline admin gate over the platform principal with a "
        "fail-closed allow-list, refuse outbound HTTP, require managed identity for Event "
        "Hubs, drop the App Configuration editor, chat stores and preset files, never "
        "publish fabricated sample metrics, and allow a configured key-ring path."
    ),
    "CompanionApp/Components/Pages/Home.razor": (
        "Offer only the Event Hub monitor and Insights; drop the routes, links and "
        "Investigator wizard of excluded tools."
    ),
    "CompanionApp/Components/Layout/NavMenu.razor": (
        "Link only the retained Event Hub monitor and Insights pages."
    ),
    "CompanionApp/Components/Shared/EventHub/EventHubReader.cs": (
        "Serialize the pipeline across the concurrent partition readers, whose shared "
        "request dictionaries upstream mutated without a lock, and drop the unbounded "
        "incomplete.json writer of raw event JSON for the excluded /incomplete page."
    ),
    "CompanionApp/Ai4ia/HostedGuard.cs": (
        "AI4IA hosted-mode guards: the admin allow-list gate, a refusing HttpClient and "
        "managed-identity-only Event Hubs."
    ),
    "CompanionApp/packages.lock.json": "AI4IA-generated NuGet lock for deterministic restore.",
}

# Upstream files deliberately not vendored, as case-sensitive fnmatch patterns ("*"
# also crosses "/"). Each excluded file is still recorded with its upstream hash so
# the omission is exact.
AI4IA_EXCLUSION_REASONS = {
    "CompanionApp/Components/Pages/AbortTestPage.razor": (
        "Disconnect tester sends caller-directed server-side requests."
    ),
    "CompanionApp/Components/Pages/ChatPage.razor": (
        "Model chat would bypass the API's admission, quota, receipts and ownership."
    ),
    "CompanionApp/Components/Pages/InvestigatorPage.razor": (
        "Sends caller-chosen URLs and headers from the server: SSRF and credential forwarding."
    ),
    "CompanionApp/Components/Pages/StressTestPage.razor": (
        "Generates concurrent caller-directed load and model cost from the server."
    ),
    "CompanionApp/Components/Pages/TestsPage.razor": (
        "Runs caller-directed request tests from the server."
    ),
    "CompanionApp/Components/Pages/UrlTesterPage.razor": (
        "Sends caller-chosen URLs and headers from the server: SSRF and credential forwarding."
    ),
    "CompanionApp/Components/Pages/AdminConfigurationPage.razor*": (
        "App Configuration editor publishes with the server identity; AI4IA keeps proxy "
        "settings in Bicep."
    ),
    "CompanionApp/Components/Pages/Deployment*": (
        "Generates upstream's own deployment topology, not AI4IA's."
    ),
    "CompanionApp/Assets/Deployment/*": "Embedded templates of the excluded deployment page.",
    "CompanionApp/wwwroot/images/azure/*": "Images of the excluded deployment page.",
    "CompanionApp/Components/Pages/HistoryPage.razor": (
        "Instance-wide chat history of the excluded chat tools."
    ),
    "CompanionApp/Components/Pages/UserPreferencesPage.razor": (
        "Lets a user point history storage at arbitrary Blob or Cosmos accounts with the "
        "server identity."
    ),
    "CompanionApp/Components/Pages/IncompletePage.razor": "Placeholder for unfinished upstream features.",
    "CompanionApp/chat-models*": (
        "Presets of the excluded chat tools; AI4IA models come from infra/models.json."
    ),
    "CompanionApp/vision-models*": (
        "Presets of the excluded vision tools; AI4IA models come from infra/models.json."
    ),
    "CompanionApp/Dockerfile*": (
        "Upstream image definition with floating base tags; AI4IA builds "
        "proxy/CompanionApp.Dockerfile."
    ),
    "CompanionApp/deploy.sh": "Upstream operator script for its own deployment.",
    "CompanionApp/make-zip.sh": "Upstream operator script for its own deployment.",
    "CompanionApp/upload_zip.sh": "Upstream operator script for its own deployment.",
    "CompanionApp/update_settings.sh": "Upstream operator script for its own deployment.",
    "CompanionApp/Properties/launchSettings.json": "Local development launch profile.",
    "CompanionApp/configuration.md": "Upstream documentation; proxy/README.md documents the AI4IA subset.",
    "CompanionApp/delpoyment.md": "Upstream documentation; proxy/README.md documents the AI4IA subset.",
    "CompanionApp/deployment-cli.md": "Upstream documentation; proxy/README.md documents the AI4IA subset.",
    "CompanionApp/readme.md": "Upstream documentation; proxy/README.md documents the AI4IA subset.",
    "CompanionApp/S7P-CircuitBreakerError_DataFlow.md": (
        "Upstream documentation; proxy/README.md documents the AI4IA subset."
    ),
    "CompanionApp/testing.md": "Upstream documentation; proxy/README.md documents the AI4IA subset.",
    "CompanionApp/todo.md": "Upstream documentation; proxy/README.md documents the AI4IA subset.",
    "CompanionApp/image*.png": "Documentation screenshot.",
    "CompanionApp/managed-identity.png": "Documentation screenshot.",
    "CompanionApp/zip-upload.png": "Documentation screenshot.",
    "CompanionApp/event.json": "Sample event data for local replay.",
    "CompanionApp/data/.gitignore": "Placeholder of the excluded disk history store.",
    "CompanionApp/wwwroot/lib/bootstrap/dist/js/*": "Bootstrap scripts the retained pages never load.",
    "CompanionApp/wwwroot/lib/bootstrap/dist/css/bootstrap-*": (
        "Bootstrap stylesheet variants the retained pages never load."
    ),
    "CompanionApp/wwwroot/lib/bootstrap/dist/css/bootstrap.css": (
        "Bootstrap stylesheet variants the retained pages never load."
    ),
    "CompanionApp/wwwroot/lib/bootstrap/dist/css/bootstrap.rtl*": (
        "Bootstrap stylesheet variants the retained pages never load."
    ),
}


def _exclusion_rule(path: str) -> str | None:
    """The single declared rule that excludes ``path``, if any."""

    matches = [rule for rule in AI4IA_EXCLUSION_REASONS if fnmatch.fnmatchcase(path, rule)]
    if len(matches) > 1:
        raise ValueError(f"{path}: matched by more than one exclusion rule: {matches}")
    return matches[0] if matches else None


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
    excluded = {path: rule for path in upstream if (rule := _exclusion_rule(path)) is not None}
    present = sorted(set(excluded).intersection(local))
    if present:
        raise ValueError(f"excluded upstream files are present locally: {present}")
    missing = sorted(set(upstream) - set(local) - set(excluded))
    if missing:
        raise ValueError(f"upstream files are missing locally: {missing}")
    unused = sorted(set(AI4IA_EXCLUSION_REASONS) - set(excluded.values()))
    if unused:
        raise ValueError(f"exclusion rules match no upstream file: {unused}")

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
    for path, rule in excluded.items():
        files[path] = {
            "disposition": "ai4ia-excluded",
            "rule": rule,
            "upstreamRawSha256": _sha256(upstream[path]),
            "upstreamCanonicalSha256": _canonical_sha256(upstream[path]),
        }

    declared = set(AI4IA_PATCH_REASONS)
    if patch_paths != declared:
        raise ValueError(
            "AI4IA patch declarations do not match measured drift: "
            f"undeclared={sorted(patch_paths - declared)}, "
            f"stale={sorted(declared - patch_paths)}"
        )

    counts = Counter(entry["disposition"] for entry in files.values())
    return {
        "schemaVersion": 3,
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
        "exclusions": [
            {"rule": rule, "reason": AI4IA_EXCLUSION_REASONS[rule]}
            for rule in sorted(AI4IA_EXCLUSION_REASONS)
        ],
        "files": dict(sorted(files.items())),
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
    excluded = {
        path for path, entry in recorded.items()
        if entry.get("disposition") == "ai4ia-excluded"
    }
    vendored = set(recorded) - excluded
    if set(local) != vendored:
        errors.append(
            "manifest coverage drift: "
            f"unrecorded={sorted(set(local) - vendored)}, "
            f"missing={sorted(vendored - set(local))}"
        )

    measured_counts: Counter[str] = Counter()
    for path in sorted(excluded):
        entry = recorded[path]
        measured_counts["ai4ia-excluded"] += 1
        if path in local:
            errors.append(f"{path}: excluded upstream file is present locally")
        try:
            rule = _exclusion_rule(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if rule is None or entry.get("rule") != rule:
            errors.append(f"{path}: exclusion is not declared by the reviewed AI4IA rules")
        if not entry.get("upstreamCanonicalSha256"):
            errors.append(f"{path}: excluded file lacks its upstream hash")
    for path in sorted(set(local).intersection(vendored)):
        entry = recorded[path]
        local_bytes = local[path]
        # A reviewed exclusion covers the path no matter how the manifest records it, so a
        # re-added file cannot pass by flipping its entry while the rule still matches others.
        try:
            rule = _exclusion_rule(path)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if rule is not None:
                errors.append(f"{path}: vendored file matches exclusion rule {rule!r}")
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

    exclusion_entries = document.get("exclusions") or []
    if {entry.get("rule"): entry.get("reason") for entry in exclusion_entries} != AI4IA_EXCLUSION_REASONS:
        errors.append("explicit exclusion list does not match the reviewed AI4IA exclusions")
    used_rules = {recorded[path].get("rule") for path in excluded}
    if used_rules != set(AI4IA_EXCLUSION_REASONS):
        errors.append(
            f"exclusion rules match no recorded file: {sorted(set(AI4IA_EXCLUSION_REASONS) - used_rules)}"
        )

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
