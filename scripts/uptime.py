"""Anonymous uptime observations and static portal publication; no model or user IO."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from scripts.http_contract import HttpError, public_origin, stamp, strict_json, timestamp
from scripts.http_transport import Response, Transport

PORTAL = "https://ian-t-adams.github.io/AI4IA"
THRESHOLD = 3
STALE_SECONDS = 45 * 60
REPORT_LIMIT = 16 * 1024
SNAPSHOT_LIMIT = 256 * 1024
REASONS = frozenset({
    "healthy", "http_error", "invalid_configuration", "invalid_response",
    "network_unavailable", "deadline", "redirect_rejected", "response_too_large",
})
ENDPOINTS = (
    ("web", "Web", "/"),
    ("proxy", "Proxy liveness", "/liveness"),
    ("api_live", "API liveness", "/health/live"),
    ("api_ready", "API readiness", "/health/ready"),
)
State = Literal["up", "down", "unknown"]
Transition = Literal["none", "firing", "recovered"]


class Observation(TypedDict):
    id: str
    url: str | None
    state: State
    reason: str
    observedAt: str | None
    httpStatus: int | None
    latencyMs: int | None
    failureStreak: int
    alerting: bool
    transition: Transition


class Report(TypedDict):
    version: int
    checkedAt: str | None
    history: str
    overall: State
    endpoints: list[Observation]


def now() -> datetime:
    return datetime.now(timezone.utc)


def origins(environment: dict[str, str]) -> dict[str, str | None]:
    values = {
        "web": "https://" + environment.get("AI4IA_WEB_CUSTOM_DOMAIN", ""),
        "proxy": "https://" + environment.get("AI4IA_PROXY_CUSTOM_DOMAIN", ""),
        "api": environment.get("AI4IA_UPTIME_API_URL", ""),
    }
    result: dict[str, str | None] = {}
    for name, value in values.items():
        try:
            result[name] = public_origin(value)
        except HttpError:
            result[name] = None
    return result


def accepted(identifier: str, response: Response) -> bool:
    if response.status != 200:
        return False
    if identifier == "web":
        return response.content_type == "text/html" and bool(response.body.strip())
    if identifier == "proxy":
        return response.content_type == "text/plain" and response.body.strip() == b"OK"
    data = response.object()
    return data.get("status") == "ok" and (
        identifier != "api_ready" or data.get("stage") == "session_store"
    )


async def probe(client: Transport, identifier: str, url: str | None) -> Observation:
    row: Observation = {
        "id": identifier, "url": url, "state": "unknown",
        "reason": "invalid_configuration", "observedAt": None,
        "httpStatus": None, "latencyMs": None, "failureStreak": 0,
        "alerting": False, "transition": "none",
    }
    if url is None:
        return row
    row["observedAt"] = stamp(now())
    started = asyncio.get_running_loop().time()
    try:
        response = await client.request(
            "GET", url, timeout=20, limit=512 * 1024 if identifier == "web" else 4096,
        )
        row["httpStatus"] = response.status
        if response.status != 200:
            row["state"], row["reason"] = "down", "http_error"
        elif accepted(identifier, response):
            row["state"], row["reason"] = "up", "healthy"
        else:
            row["reason"] = "invalid_response"
    except HttpError as exc:
        row["reason"] = exc.code
    row["latencyMs"] = round((asyncio.get_running_loop().time() - started) * 1000)
    return row


def parse_report(raw: bytes) -> Report:
    value = strict_json(raw, limit=REPORT_LIMIT)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "checkedAt", "history", "overall", "endpoints"}
        or type(value["version"]) is not int or value["version"] != 1
        or value["overall"] not in ("up", "down", "unknown")
        or value["history"] not in ("available", "unavailable", "stale", "scope_changed")
        or not isinstance(value["endpoints"], list)
        or len(value["endpoints"]) != len(ENDPOINTS)
    ):
        raise HttpError("invalid_response")
    timestamp(value["checkedAt"])
    rows: list[Observation] = []
    for item, (identifier, _, path) in zip(value["endpoints"], ENDPOINTS, strict=True):
        if (
            not isinstance(item, dict) or set(item) != set(Observation.__annotations__)
            or item["id"] != identifier or item["state"] not in ("up", "down", "unknown")
            or item["transition"] not in ("none", "firing", "recovered")
            or type(item["failureStreak"]) is not int
            or not 0 <= item["failureStreak"] <= THRESHOLD
            or type(item["alerting"]) is not bool
            or item["alerting"] != (item["failureStreak"] == THRESHOLD)
            or (item["state"] == "up" and item["failureStreak"] != 0)
            or not isinstance(item["reason"], str) or item["reason"] not in REASONS
            or (item["state"] != "up" and item["failureStreak"] == 0)
            or (item["state"] == "up" and (
                item["reason"] != "healthy" or item["httpStatus"] != 200
                or item["url"] is None or item["observedAt"] is None
            ))
        ):
            raise HttpError("invalid_response")
        if item["url"] is not None:
            try:
                parsed = urlsplit(item["url"]) if isinstance(item["url"], str) else None
                if parsed is None or item["url"] != public_origin(f"https://{parsed.netloc}") + path:
                    raise HttpError("invalid_response")
            except ValueError as exc:
                raise HttpError("invalid_response") from exc
        if item["observedAt"] is not None:
            timestamp(item["observedAt"])
        for field in ("latencyMs", "httpStatus"):
            number = item[field]
            maximum = 30_000 if field == "latencyMs" else 599
            if number is not None and (type(number) is not int or not 0 <= number <= maximum):
                raise HttpError("invalid_response")
        rows.append(Observation(
            id=identifier, url=item["url"], state=item["state"], reason=item["reason"],
            observedAt=item["observedAt"], httpStatus=item["httpStatus"],
            latencyMs=item["latencyMs"], failureStreak=item["failureStreak"],
            alerting=item["alerting"], transition=item["transition"],
        ))
    return Report(
        version=1, checkedAt=value["checkedAt"], history=value["history"],
        overall=value["overall"], endpoints=rows,
    )


def finish(rows: list[Observation], previous: Report | None, observed: datetime) -> Report:
    history = "unavailable"
    prior: list[Observation] = []
    if previous is not None:
        age = (observed - timestamp(previous["checkedAt"])).total_seconds()
        history = "available" if 0 <= age <= STALE_SECONDS else "stale"
        if history == "available":
            prior = previous["endpoints"]
    for index, row in enumerate(rows):
        old = prior[index] if prior else None
        if old is not None and old["url"] != row["url"]:
            old, history = None, "scope_changed"
        row["failureStreak"] = (
            0 if row["state"] == "up"
            else min(THRESHOLD, (old["failureStreak"] if old else 0) + 1)
        )
        row["alerting"] = row["failureStreak"] == THRESHOLD
        was_alerting = old["alerting"] if old else False
        row["transition"] = (
            "firing" if row["alerting"] and not was_alerting
            else "recovered" if row["state"] == "up" and was_alerting else "none"
        )
    states = {row["state"] for row in rows}
    overall: State = "down" if "down" in states else "unknown" if "unknown" in states else "up"
    return Report(
        version=1, checkedAt=stamp(observed), history=history, overall=overall, endpoints=rows,
    )


async def previous_report(client: Transport) -> Report | None:
    try:
        response = await client.request(
            "GET", data_url("uptime.json"), timeout=5, limit=REPORT_LIMIT,
        )
        if response.status != 200 or response.content_type != "application/json":
            raise HttpError("invalid_response")
        return parse_report(response.body)
    except HttpError as exc:
        print(f"::warning::Uptime alert history unavailable ({exc.code}); current checks will still run.")
        return None


def data_url(name: str) -> str:
    return f"{PORTAL}/data/{name}?t={int(now().timestamp())}"


async def carry_snapshots(client: Transport, directory: Path) -> None:
    for name, variable in (("inventory", "AI4IA_INVENTORY"), ("status", "AI4IA_STATUS")):
        data: Any = None
        try:
            response = await client.request(
                "GET", data_url(f"{name}.js"), timeout=5, limit=SNAPSHOT_LIMIT,
            )
            if response.status != 200:
                raise HttpError("invalid_response")
            match = re.fullmatch(
                rf"\s*(?://[^\n]*\n)?window\.{variable}\s*=\s*(\{{[\s\S]*\}})\s*;\s*",
                response.body.decode("utf-8-sig"),
            )
            if match is None:
                raise HttpError("invalid_response")
            data = strict_json(match[1].encode("utf-8"), limit=SNAPSHOT_LIMIT)
            if not isinstance(data, dict) or not isinstance(data.get("generatedAt"), str):
                raise HttpError("invalid_response")
            generated = datetime.fromisoformat(data["generatedAt"].replace("Z", "+00:00"))
            if generated.tzinfo is None or generated > now():
                raise HttpError("invalid_response")
            if name == "inventory" and not isinstance(data.get("resources"), list):
                raise HttpError("invalid_response")
            if name == "status" and not isinstance(data.get("summary"), dict):
                raise HttpError("invalid_response")
        except (HttpError, ValueError, UnicodeError):
            data = None
            print(f"::warning::Published Azure {name} snapshot unavailable; no committed seed will be substituted.")
        (directory / f"{name}.js").write_text(
            f"window.{variable} = {json.dumps(data, ensure_ascii=True, allow_nan=False)};\n",
            encoding="utf-8",
        )


def write_report(directory: Path, report: Report | None) -> None:
    missing = {
        "version": 1, "checkedAt": None, "history": "unavailable",
        "overall": "unknown", "endpoints": [],
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "uptime.json").write_text(
        json.dumps(report or missing, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8",
    )


def publish_summary(report: Report) -> None:
    lines = [
        "## Anonymous uptime observations",
        f"Checked: {report['checkedAt']}. Alert history: {report['history']}.",
        "",
        "| Endpoint | Observation | HTTP | Latency (ms) | Failure streak | Transition |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    names = {identifier: label for identifier, label, _ in ENDPOINTS}
    for row in report["endpoints"]:
        lines.append(
            f"| {names[row['id']]} | {row['state']} ({row['reason']}) | "
            f"{row['httpStatus']} | {row['latencyMs']} | {row['failureStreak']} | {row['transition']} |"
        )
    lines.extend([
        "", "Collection success is not application health. Only a new three-failure incident fails the",
        "notification job; ongoing outages remain visible here and on the status page without repeat alerts.",
        "A passing observation automatically clears that endpoint's incident. Unknown is not healthy.",
    ])
    text = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as target:
            target.write(text)
    print(text)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as target:
            target.write(
                "firing=" + str(any(row["transition"] == "firing" for row in report["endpoints"])).lower() + "\n"
            )
            target.write(
                "recovered=" + str(any(row["transition"] == "recovered" for row in report["endpoints"])).lower() + "\n"
            )


async def run(operation: str, directory: Path, environment: dict[str, str]) -> None:
    targets = origins(environment)
    allowed = {public_origin(f"https://{urlsplit(PORTAL).netloc}")}
    allowed.update(value for value in targets.values() if value is not None)
    async with Transport(allowed) as client:
        previous = await previous_report(client)
        if operation == "carry":
            write_report(directory, previous)
            return
        requests = []
        for identifier, _, path in ENDPOINTS:
            origin = targets["api" if identifier.startswith("api_") else identifier]
            requests.append(probe(client, identifier, origin + path if origin else None))
        rows = await asyncio.gather(*requests)
        report = finish(rows, previous, now())
        write_report(directory, report)
        publish_summary(report)
        await carry_snapshots(client, directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("observe", "carry"))
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.operation, arguments.directory, dict(os.environ)))


if __name__ == "__main__":
    main()
