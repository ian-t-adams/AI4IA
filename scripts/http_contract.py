"""Dependency-free bounded JSON and public-origin contracts for operational probes."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

MAX_HTTP_BYTES = 256 * 1024
MAX_REPORT_BYTES = 32 * 1024
ERRORS = frozenset({
    "invalid_configuration", "invalid_response", "network_unavailable",
    "deadline", "redirect_rejected", "response_too_large",
})


class HttpError(Exception):
    """Expose only a fixed outcome code, never response or exception text."""

    def __init__(self, code: str) -> None:
        if code not in ERRORS:
            raise ValueError("Unregistered HTTP outcome code.")
        super().__init__(code)
        self.code = code


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise HttpError("invalid_response")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HttpError("invalid_response") from exc


def obj(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HttpError("invalid_response")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HttpError("invalid_response")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise HttpError("invalid_response")


def strict_json(raw: bytes, *, limit: int = MAX_REPORT_BYTES) -> Any:
    if len(raw) > limit:
        raise HttpError("response_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HttpError("invalid_response") from exc
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 20_000:
            raise HttpError("invalid_response")
        if isinstance(item, float) and not math.isfinite(item):
            raise HttpError("invalid_response")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def encoded(value: Any, *, limit: int = MAX_REPORT_BYTES) -> bytes:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")
    strict_json(raw, limit=limit)
    return raw


def public_origin(value: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        raise HttpError("invalid_configuration")
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port in (None, 443)
    except ValueError as exc:
        raise HttpError("invalid_configuration") from exc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https" or not valid_port
        or parsed.netloc != host or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment or parsed.username is not None
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
        or "." not in host or ".." in host or host.endswith((".localhost", ".local"))
    ):
        raise HttpError("invalid_configuration")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}"
    raise HttpError("invalid_configuration")
