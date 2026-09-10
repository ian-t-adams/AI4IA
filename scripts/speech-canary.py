#!/usr/bin/env python3
"""Opt-in, one-request PCM/WAV canary through the authenticated app speech API.

No token acquisition, direct model/gateway request, playback, saved audio, retry,
or deployment operation. Without --execute, only the catalog target is reported.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import struct
import subprocess
import sys
from collections.abc import AsyncIterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "infra" / "models.json"
MAX_CATALOG_BYTES = 1024 * 1024
MAX_AUDIO_BYTES = 1_000_000
MAX_AUDIO_SECONDS = 15
MIN_AUDIO_MS = 100
MAX_REPORT_BYTES = 4096
MAX_TIMEOUT = 30
SPEECH_PATH = "/api/voice/speech"
TOKEN_ENV = "AI4IA_SPEECH_CANARY_TOKEN"
SYNTHETIC_TEXT = "This is the AI4IA speech canary."
SCHEMA = "ai4ia-speech-canary-v1"
OUTCOMES = frozenset({
    "not_run", "success", "http_error", "invalid_headers", "oversized_audio",
    "invalid_audio", "timeout", "connection_error", "configuration_error",
    "dependency_missing", "worker_error",
})
_SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class CanaryInputError(ValueError):
    """An unsafe or unavailable operator target; messages contain no input."""


class CanaryResponseError(ValueError):
    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass(frozen=True)
class Target:
    model: str
    region: str
    catalogVersion: str
    catalogSha256: str


def validate_url(value: str) -> str:
    if len(value) > 512 or re.search(r"\s|[\x00-\x1f\x7f]", value):
        raise CanaryInputError("Use an explicit HTTPS app speech URL without whitespace.")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CanaryInputError("Invalid app speech URL port.") from exc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https" or port not in (None, 443)
        or parsed.username is not None or parsed.password is not None
        or parsed.path != SPEECH_PATH or parsed.query or parsed.fragment
        or not host.isascii() or len(host) > 253 or "." not in host
        or any(not _DNS_LABEL.fullmatch(label) for label in host.split("."))
        or host.endswith((".localhost", ".local", ".azure-api.net", ".openai.azure.com",
                          ".services.ai.azure.com", ".cognitiveservices.azure.com"))
    ):
        raise CanaryInputError("Use the HTTPS app /api/voice/speech URL, not a provider or gateway URL.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return urlunsplit(("https", host, SPEECH_PATH, "", ""))
    raise CanaryInputError("The app speech URL must use a DNS host, not an IP address.")


def resolve_target(model: str, region: str) -> Target:
    if not _SELECTOR.fullmatch(model) or not _SELECTOR.fullmatch(region):
        raise CanaryInputError("Model and region must be catalog identifiers.")
    with CATALOG.open("rb") as stream:
        data = stream.read(MAX_CATALOG_BYTES + 1)
    if len(data) > MAX_CATALOG_BYTES:
        raise CanaryInputError("Model catalog exceeds the input bound.")
    source = json.loads(data)
    matches = [
        deployment for entry in source["catalog"]
        if entry["name"] == model and entry["category"] == "tts"
        and entry.get("runtimeEnabled", True) is True
        for deployment in entry["deployments"] if deployment["region"] == region
    ]
    if len(matches) != 1 or not _SELECTOR.fullmatch(matches[0]["version"]):
        raise CanaryInputError("Select exactly one catalog TTS deployment in the requested region.")
    return Target(model, region, matches[0]["version"], hashlib.sha256(data).hexdigest())


def inspect_wav(audio: bytes) -> dict[str, int]:
    """Validate bounded PCM chunks, including streaming length sentinels."""
    if not 44 <= len(audio) <= MAX_AUDIO_BYTES or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise CanaryResponseError("invalid_audio")
    riff_size = struct.unpack_from("<I", audio, 4)[0]
    streaming = riff_size == 0xFFFFFFFF
    if not streaming and riff_size + 8 != len(audio):
        raise CanaryResponseError("invalid_audio")
    offset, count = 12, 0
    pcm: tuple[int, int, int] | None = None
    result: dict[str, int] | None = None
    while offset < len(audio):
        count += 1
        if count > 32 or len(audio) - offset < 8:
            raise CanaryResponseError("invalid_audio")
        kind = audio[offset:offset + 4]
        size = struct.unpack_from("<I", audio, offset + 4)[0]
        start = offset + 8
        if size == 0xFFFFFFFF:
            if not streaming or kind != b"data":
                raise CanaryResponseError("invalid_audio")
            size = len(audio) - start
        end = start + size
        offset = end + size % 2
        if offset > len(audio):
            raise CanaryResponseError("invalid_audio")
        if kind == b"fmt ":
            if pcm is not None or size not in (16, 18):
                raise CanaryResponseError("invalid_audio")
            encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from(
                "<HHIIHH", audio, start,
            )
            if (
                encoding != 1 or channels not in (1, 2) or not 8000 <= rate <= 48000
                or bits != 16 or alignment != channels * 2 or byte_rate != rate * alignment
                or (size == 18 and audio[start + 16:end] != b"\0\0")
            ):
                raise CanaryResponseError("invalid_audio")
            pcm = channels, rate, alignment
        elif kind == b"data":
            if pcm is None or result is not None:
                raise CanaryResponseError("invalid_audio")
            channels, rate, alignment = pcm
            if (
                size % alignment or size * 1000 < rate * alignment * MIN_AUDIO_MS
                or size > rate * alignment * MAX_AUDIO_SECONDS
            ):
                raise CanaryResponseError("invalid_audio")
            result = {
                "bytes": len(audio), "channels": channels, "sampleRateHz": rate,
                "frames": size // alignment, "durationMs": size * 1000 // (rate * alignment),
            }
    if result is None:
        raise CanaryResponseError("invalid_audio")
    return result


async def inspect_response(
    status: int, raw_headers: Sequence[tuple[bytes, bytes]], chunks: AsyncIterable[bytes],
    *, model: str,
) -> dict[str, int]:
    if status != 200:
        raise CanaryResponseError("http_error")
    if len(raw_headers) > 64 or sum(len(k) + len(v) for k, v in raw_headers) > 8192:
        raise CanaryResponseError("invalid_headers")
    headers: dict[bytes, bytes] = {}
    for name, value in raw_headers:
        name = name.lower()
        if name not in {b"content-type", b"content-length", b"content-encoding", b"x-model"}:
            continue
        if name in headers:
            raise CanaryResponseError("invalid_headers")
        headers[name] = value.strip()
    if (
        headers.get(b"content-type", b"").lower() != b"audio/wav"
        or headers.get(b"content-encoding", b"").lower() not in (b"", b"identity")
        or headers.get(b"x-model") != model.encode("ascii")
    ):
        raise CanaryResponseError("invalid_headers")
    declared = headers.get(b"content-length")
    if declared is not None and (
        not re.fullmatch(rb"[0-9]{1,7}", declared) or not 1 <= int(declared) <= MAX_AUDIO_BYTES
    ):
        raise CanaryResponseError("invalid_headers")
    audio = bytearray()
    async for chunk in chunks:
        if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
            raise CanaryResponseError("oversized_audio")
        audio.extend(chunk)
    if declared is not None and int(declared) != len(audio):
        raise CanaryResponseError("invalid_audio")
    return inspect_wav(bytes(audio))


def result(target: Target, outcome: str, *, http_status: int | None = None,
           audio: dict[str, int] | None = None) -> dict:
    return {
        "schema": SCHEMA, "outcome": outcome, "target": asdict(target),
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "modelVersionEvidence": "declared-only", "httpStatus": http_status, "audio": audio,
    }


async def run_canary(url: str, target: Target, token: str, timeout: float) -> dict:
    try:
        import aiohttp
    except ImportError:
        return result(target, "dependency_missing")
    status = None
    try:
        async with asyncio.timeout(timeout):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                trust_env=False, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
                max_line_size=8192, max_field_size=8192, read_bufsize=8192,
            ) as client:
                async with client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "audio/wav",
                             "Accept-Encoding": "identity", "Cache-Control": "no-store"},
                    json={"input": SYNTHETIC_TEXT, "model": target.model, "region": target.region,
                          "voice": "alloy", "format": "wav"},
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    audio = await inspect_response(
                        status, response.raw_headers, response.content.iter_chunked(8192),
                        model=target.model,
                    )
                    return result(target, "success", http_status=status, audio=audio)
    except CanaryResponseError as exc:
        return result(target, exc.outcome, http_status=status)
    except TimeoutError:
        return result(target, "timeout", http_status=status)
    except (aiohttp.ClientError, OSError):
        # Transport exceptions can contain headers; only a closed outcome leaves the worker.
        return result(target, "connection_error", http_status=status)


def run_bounded(args: argparse.Namespace, target: Target) -> dict:
    command = [
        sys.executable, str(Path(__file__).resolve()), "--execute", "--worker",
        "--url", args.url, "--model", target.model, "--region", target.region,
        "--token-env", args.token_env, "--timeout", str(args.timeout),
        "--expected-catalog-sha256", target.catalogSha256,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, check=False, timeout=args.timeout + 5,
        )
    except subprocess.TimeoutExpired:
        # Also bounds blocked DNS and event-loop shutdown. No retry: the request
        # might already have been accepted and billed when the worker is killed.
        return result(target, "timeout")
    except OSError:
        return result(target, "worker_error")
    if len(completed.stdout) > MAX_REPORT_BYTES:
        return result(target, "worker_error")
    try:
        reported = json.loads(completed.stdout)
    except (ValueError, UnicodeError):
        return result(target, "worker_error")
    if (
        not isinstance(reported, dict)
        or reported.get("schema") != SCHEMA or reported.get("target") != asdict(target)
        or not isinstance(reported.get("outcome"), str) or reported["outcome"] not in OUTCOMES
        or not isinstance(reported.get("observedAt"), str)
        or re.fullmatch(r"[0-9TZ:.-]{20,32}", reported["observedAt"]) is None
        or reported.get("modelVersionEvidence") != "declared-only"
        or set(reported) != set(result(target, "worker_error"))
        or (reported["outcome"] == "success") != (completed.returncode == 0)
    ):
        return result(target, "worker_error")
    status, audio = reported["httpStatus"], reported["audio"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        return result(target, "worker_error")
    if reported["outcome"] == "success":
        if (
            status != 200 or not isinstance(audio, dict)
            or set(audio) != {"bytes", "channels", "sampleRateHz", "frames", "durationMs"}
            or any(type(value) is not int for value in audio.values())
            or not 44 <= audio["bytes"] <= MAX_AUDIO_BYTES
            or audio["channels"] not in (1, 2) or not 8000 <= audio["sampleRateHz"] <= 48000
            or not MIN_AUDIO_MS <= audio["durationMs"] <= MAX_AUDIO_SECONDS * 1000
            or audio["frames"] * 1000 // audio["sampleRateHz"] != audio["durationMs"]
            or not 0 < audio["frames"] * audio["channels"] * 2 <= audio["bytes"] - 44
        ):
            return result(target, "worker_error")
    elif audio is not None:
        return result(target, "worker_error")
    return reported


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True, help="Catalog TTS model ID.")
    parser.add_argument("--region", required=True, help="Exact catalog deployment region.")
    parser.add_argument("--token-env", default=TOKEN_ENV, help="Explicit Entra token variable; never acquired.")
    parser.add_argument("--timeout", type=float, default=MAX_TIMEOUT, help="Request deadline, 1-30 seconds.")
    parser.add_argument("--execute", action="store_true", help="Make one approved, billable app request.")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-catalog-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        args.url = validate_url(args.url)
        if not _ENV_NAME.fullmatch(args.token_env) or not 1 <= args.timeout <= MAX_TIMEOUT:
            raise CanaryInputError("Invalid token variable name or timeout outside 1-30 seconds.")
        target = resolve_target(args.model, args.region)
        if args.expected_catalog_sha256 and args.expected_catalog_sha256 != target.catalogSha256:
            raise CanaryInputError("The catalog changed before execution; approval must be rechecked.")
    except (CanaryInputError, OSError, ValueError, KeyError) as exc:
        parser.error(str(exc) if isinstance(exc, CanaryInputError) else "Cannot resolve the bounded catalog target.")

    if not args.execute:
        report = result(target, "not_run")
    elif args.worker:
        token = os.environ.get(args.token_env, "")
        if not token or len(token) > 16_384 or not _TOKEN.fullmatch(token):
            report = result(target, "configuration_error")
        else:
            report = asyncio.run(run_canary(args.url, target, token, args.timeout))
    else:
        report = run_bounded(args, target)
    print(json.dumps(report, separators=(",", ":"), ensure_ascii=True))
    return 0 if report["outcome"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
