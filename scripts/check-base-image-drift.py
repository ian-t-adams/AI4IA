#!/usr/bin/env python3
"""Report public base-tag index drift without changing pins or pulling image layers.

Exit 0: all observed indexes match. Exit 1: drift. Exit 2: incomplete/unknown.
Only anonymous Docker Hub and Microsoft Container Registry metadata are queried.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import parse_http_list, parse_keqv_list

from _base_image_refs import (
    PINNED_REF,
    BaseSourceError,
    external_references,
    tracked_dockerfiles,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024
OBSERVATION_TIMEOUT = 30
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*\Z")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True)
class Pin:
    image: str
    registry: str
    repository: str
    tag: str
    digest: str
    sources: tuple[str, ...] = ()

    @property
    def reference(self) -> str:
        return f"{self.image}@{self.digest}"


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


class ObservationError(ValueError):
    """A fixed safe reason, never a server body, credential or raw exception."""


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_pin(reference: str) -> Pin:
    match = PINNED_REF.fullmatch(reference)
    if not match or len(reference) > 512 or not TAG.fullmatch(match.group("tag")):
        raise BaseSourceError("invalid_base_pin")
    name = match.group("name")
    if name.startswith("mcr.microsoft.com/"):
        registry = "mcr.microsoft.com"
        repository = name.removeprefix("mcr.microsoft.com/")
        display = registry
    else:
        registry = "registry-1.docker.io"
        display = "docker.io"
        explicitly_hub = name.startswith("docker.io/")
        repository = name.removeprefix("docker.io/")
        if "/" not in repository:
            repository = f"library/{repository}"
        elif not explicitly_hub and (
            "." in repository.split("/", 1)[0] or ":" in repository
            or repository.split("/", 1)[0] == "localhost"
        ):
            raise BaseSourceError("unsupported_public_registry")
    if not NAME.fullmatch(repository):
        raise BaseSourceError("invalid_base_pin")
    tag = match.group("tag")
    return Pin(f"{display}/{repository}:{tag}", registry, repository, tag,
               f"sha256:{match.group('digest')}")


def collect_pins(root: Path) -> list[Pin]:
    pins: dict[str, Pin] = {}
    source_count = 0
    for name in tracked_dockerfiles(root):
        for line, reference in external_references(root / name):
            pin = parse_pin(reference)
            source_count += 1
            existing = pins.get(pin.reference, pin)
            pins[pin.reference] = replace(existing, sources=(*existing.sources, f"{name}:{line}"))
            if len(pins) > 32 or source_count > 128:
                raise BaseSourceError("base_inventory_out_of_bounds")
    if not pins:
        raise BaseSourceError("no_external_bases")
    return sorted(pins.values(), key=lambda pin: pin.reference)


def https_get(url: str, headers: dict[str, str], limit: int) -> HttpResult:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"registry-1.docker.io", "mcr.microsoft.com", "auth.docker.io"}
        or parsed.fragment
    ):
        raise ObservationError("unapproved_metadata_endpoint")
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=10)
    try:
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.request("GET", target, headers={
            "User-Agent": "AI4IA-base-image-drift/1",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            **headers,
        })
        response = connection.getresponse()
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ObservationError("response_too_large")
        selected = {
            name: ", ".join(response.headers.get_all(name, []))
            for name in ("content-type", "content-encoding", "docker-content-digest", "www-authenticate")
        }
        return HttpResult(response.status, selected, body)
    finally:
        connection.close()


def strict_json(body: bytes) -> dict:
    def reject_constant(_value: str) -> None:
        raise ValueError("non_json_constant")

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_field")
            result[key] = value
        return result

    value = json.loads(body.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ObservationError("metadata_object_required")
    return value


def authenticate(pin: Pin, challenge: str, get: Callable) -> str:
    if pin.registry != "registry-1.docker.io" or not challenge.lower().startswith("bearer "):
        raise ObservationError("unsupported_authentication")
    fields = parse_http_list(challenge[7:])
    parameters = parse_keqv_list(fields)
    expected = {
        "realm": "https://auth.docker.io/token",
        "service": "registry.docker.io",
        "scope": f"repository:{pin.repository}:pull",
    }
    if len(fields) != 3 or parameters != expected:
        raise ObservationError("unapproved_authentication_challenge")
    query = urlencode({"service": expected["service"], "scope": expected["scope"]})
    response = get(f"https://auth.docker.io/token?{query}", {"Accept": "application/json"}, 64 * 1024)
    if response.status != 200:
        raise ObservationError("anonymous_authentication_unavailable")
    token_data = strict_json(response.body)
    token = token_data.get("token", token_data.get("access_token"))
    if (
        not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,16384}={0,2}", token)
        or ("token" in token_data and "access_token" in token_data and token_data["token"] != token_data["access_token"])
    ):
        raise ObservationError("invalid_anonymous_token")
    return token


def validate_index(response: HttpResult) -> tuple[str, str, list[str]]:
    if response.status != 200:
        raise ObservationError("registry_http_error")
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in INDEX_TYPES or response.headers.get("content-encoding", "") not in {"", "identity"}:
        raise ObservationError("index_response_required")
    document = strict_json(response.body)
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
        raise ObservationError("invalid_index_schema")
    if document.get("mediaType") != media_type:
        raise ObservationError("contradictory_index_media_type")
    manifests = document.get("manifests")
    if not isinstance(manifests, list) or not 2 <= len(manifests) <= 256:
        raise ObservationError("multi_platform_index_required")
    platforms: set[str] = set()
    for manifest in manifests:
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("mediaType"), str)
            or manifest["mediaType"] not in MANIFEST_TYPES
            or not isinstance(manifest.get("digest"), str) or not DIGEST.fullmatch(manifest["digest"])
            or type(manifest.get("size")) is not int or manifest["size"] <= 0
        ):
            raise ObservationError("invalid_platform_descriptor")
        if "platform" not in manifest:
            continue
        platform = manifest["platform"]
        if not isinstance(platform, dict):
            raise ObservationError("invalid_platform_identity")
        os_name, architecture = platform.get("os"), platform.get("architecture")
        fields = [os_name, architecture, platform.get("variant", ""), platform.get("os.version", "")]
        if any(
            not isinstance(value, str) or len(value) > 128
            or (value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value))
            for value in fields
        ) or not os_name or not architecture:
            raise ObservationError("invalid_platform_identity")
        if os_name == "unknown" or architecture == "unknown":
            continue
        label = f"{os_name}/{architecture}"
        if fields[2]:
            label += f"/{fields[2]}"
        if fields[3]:
            label += f"@{fields[3]}"
        platforms.add(label)
    if len(platforms) < 2:
        raise ObservationError("multi_platform_index_required")
    digest = f"sha256:{hashlib.sha256(response.body).hexdigest()}"
    declared = response.headers.get("docker-content-digest", "")
    if declared and declared != digest:
        raise ObservationError("contradictory_registry_digest")
    return digest, media_type, sorted(platforms)


def observe(pin: Pin, get: Callable = https_get) -> dict:
    result = {"observedAt": timestamp(), "observedDigest": None, "mediaType": None, "platforms": [], "error": None}
    try:
        url = f"https://{pin.registry}/v2/{pin.repository}/manifests/{pin.tag}"
        headers = {"Accept": ", ".join(sorted(INDEX_TYPES))}
        response = get(url, headers, MAX_MANIFEST_BYTES)
        if response.status == 401:
            token = authenticate(pin, response.headers.get("www-authenticate", ""), get)
            response = get(url, {**headers, "Authorization": f"Bearer {token}"}, MAX_MANIFEST_BYTES)
        digest, media_type, platforms = validate_index(response)
        result.update(observedDigest=digest, mediaType=media_type, platforms=platforms)
    except ObservationError as exc:
        result["error"] = str(exc)
    except (ValueError, UnicodeError, RecursionError):
        result["error"] = "invalid_metadata_json"
    except (OSError, http.client.HTTPException):
        result["error"] = "metadata_transport_unavailable"
    return result


def observe_bounded(pin: Pin) -> dict:
    # A socket timeout alone does not bound a peer that continuously drips bytes.
    # Isolate each observation so its whole auth/body flow has a wall-clock limit.
    unknown = {"observedAt": timestamp(), "observedDigest": None, "mediaType": None, "platforms": []}
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--observe", pin.reference],
            capture_output=True, timeout=OBSERVATION_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return {**unknown, "error": "observation_deadline_exceeded"}
    except OSError:
        return {**unknown, "error": "observation_process_unavailable"}
    if process.returncode != 0 or len(process.stdout) > MAX_REPORT_BYTES:
        return {**unknown, "error": "observation_process_failed"}
    try:
        result = strict_json(process.stdout)
        digest, error = result.get("observedDigest"), result.get("error")
        if (
            not isinstance(result.get("observedAt"), str)
            or not isinstance(result.get("platforms"), list)
            or not (error is None or (isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error)))
            or (error is None and (
                not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                or not isinstance(result.get("mediaType"), str) or result["mediaType"] not in INDEX_TYPES
                or len(result["platforms"]) < 2
            ))
        ):
            raise ValueError("invalid_observation")
        if datetime.fromisoformat(result["observedAt"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("observation_timezone_required")
        return {key: result.get(key) for key in (*unknown, "error")}
    except (ValueError, UnicodeError, RecursionError):
        return {**unknown, "error": "invalid_observation_result"}


def make_report(pins: list[Pin], observer: Callable = observe_bounded) -> dict:
    rows = []
    for pin in pins:
        observation = observer(pin)
        state = "unknown" if observation["error"] else (
            "current" if observation["observedDigest"] == pin.digest else "changed"
        )
        rows.append({
            "image": pin.image, "pinnedDigest": pin.digest, "sources": list(pin.sources),
            **observation, "status": state,
        })
    status = "unknown" if not rows or any(row["status"] == "unknown" for row in rows) else (
        "changed" if any(row["status"] == "changed" for row in rows) else "current"
    )
    return {"schemaVersion": 1, "generatedAt": timestamp(), "status": status, "bases": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--observe", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.observe:
        try:
            observation = observe(parse_pin(args.observe))
        except BaseSourceError:
            observation = {"error": "invalid_base_pin"}
        print(json.dumps(observation, ensure_ascii=True))
        return 0
    try:
        report = make_report(collect_pins(ROOT))
    except BaseSourceError as exc:
        report = {"schemaVersion": 1, "generatedAt": timestamp(), "status": "unknown", "bases": [], "error": str(exc)}
    serialized = json.dumps(report, ensure_ascii=True, indent=2)
    if len(serialized.encode("utf-8")) > MAX_REPORT_BYTES:
        report = {"schemaVersion": 1, "generatedAt": timestamp(), "status": "unknown", "bases": [], "error": "report_too_large"}
        serialized = json.dumps(report)
    if args.format == "json":
        print(serialized)
    else:
        print(f"Base image index drift: {report['status']} ({report['generatedAt']})")
        if report.get("error"):
            print(f"Coverage: {report['error']}")
        for row in report["bases"]:
            print(f"{row['status'].upper()}: {row['image']}")
            print(f"  pinned:   {row['pinnedDigest']}")
            print(f"  observed: {row['observedDigest'] or 'unknown'} ({row['observedAt']})")
            if row["error"]:
                print(f"  coverage: {row['error']}")
    return {"current": 0, "changed": 1, "unknown": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
