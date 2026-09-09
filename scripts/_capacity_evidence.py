"""Bounded, read-only management/aggregate-metric evidence for capacity review."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
ARM = "https://management.azure.com"
COGNITIVE_API = "2024-10-01"
METRICS_API = "2023-10-01"
DEFINITIONS_API = "2018-01-01"
NAMESPACE = "Microsoft.CognitiveServices/accounts"
METRICS = ("ModelRequests", "InputTokens", "OutputTokens", "TotalTokens")
DIMENSIONS = ("ModelDeploymentName", "ModelName", "ModelVersion", "Region")
MAX_REGIONS = 8
MAX_ACCOUNTS = 64
MAX_ACCOUNT_PAGES = 64
MAX_ACCOUNT_LINK_BYTES = 8192
MAX_ACCOUNT_CURSOR_BYTES = 4096
MAX_DEPLOYMENTS = 256
MAX_MODELS = 128
MAX_ROWS = 2048
MAX_SERIES = 256
MAX_POINTS = 168
MAX_TOTAL_POINTS = 200_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_CALLS = 192
SOURCE_SECONDS = 20
COLLECTION_SECONDS = 300
MAX_NUMBER = 2**53 - 1
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
REGION = re.compile(r"[a-z][a-z0-9]{0,31}\Z")
UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


class EvidenceError(ValueError):
    """Only fixed codes cross the public report boundary."""

    def __init__(self, code: str, response_bytes: int | None = None):
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "invalid_evidence"
        self.response_bytes = response_bytes
        super().__init__(self.code)


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not UTC_TIME.fullmatch(value):
        raise EvidenceError("invalid_utc_timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError("invalid_utc_timestamp") from None


def token(value: object) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise EvidenceError("invalid_metadata_identity")
    return value


def provider_format(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}", value):
        raise EvidenceError("invalid_model_format")
    return value


def number(value: object) -> int:
    if (
        type(value) not in (int, float)
        or not 0 <= value <= MAX_NUMBER
        or not math.isfinite(value)
        or int(value) != value
    ):
        raise EvidenceError("invalid_numeric_value")
    return int(value)


def strict_json(body: bytes) -> object:
    def reject_constant(_value: str) -> None:
        raise EvidenceError("invalid_json")

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("duplicate_json_field")
            result[key] = value
        return result

    try:
        return json.loads(body, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise EvidenceError("invalid_json") from None


def read_json(path: Path, limit: int) -> tuple[object, str]:
    try:
        with path.open("rb") as stream:
            body = stream.read(limit + 1)
    except OSError:
        raise EvidenceError("local_evidence_unavailable") from None
    if len(body) > limit:
        raise EvidenceError("local_evidence_too_large")
    return strict_json(body), hashlib.sha256(body).hexdigest()


def object_value(value: object) -> dict:
    if not isinstance(value, dict):
        raise EvidenceError("invalid_metadata_shape")
    return value


def array(value: object, limit: int) -> list:
    if not isinstance(value, list):
        raise EvidenceError("invalid_metadata_shape")
    if len(value) > limit:
        raise EvidenceError("row_limit_exceeded")
    return value


def rows(document: object, limit: int = MAX_ROWS) -> list:
    return array(object_value(document).get("value"), limit)


def subscription_id(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError
        return value.lower()
    except ValueError:
        raise EvidenceError("invalid_subscription") from None


@dataclass(frozen=True, order=True)
class Model:
    format: str
    name: str
    version: str
    sku: str

    def public(self) -> dict:
        return {"format": self.format, "name": self.name, "version": self.version, "sku": self.sku}


@dataclass(frozen=True)
class Deployment:
    name: str
    region: str
    model: Model
    baseline: int
    maximum: int | None
    declared_pool: str | None

    def public(self) -> dict:
        return {
            "name": self.name, "region": self.region, "model": self.model.public(),
            "baselineCapacity": self.baseline, "maximumCapacity": self.maximum,
            "declaredPool": self.declared_pool,
            "poolAuthority": "unverified_catalog_declaration",
            "declaredAt": None,
        }


@dataclass(frozen=True)
class Catalog:
    regions: dict[str, str]
    foundry_token: str
    deployments: tuple[Deployment, ...]
    digest: str


def load_catalog(path: Path) -> Catalog:
    raw, digest = read_json(path, MAX_CATALOG_BYTES)
    document = object_value(raw)
    naming = object_value(document.get("naming"))
    foundry_token = token(naming.get("foundryToken"))
    token(naming.get("subscriptionToken"))
    short = object_value(naming.get("skuShort"))
    for key, value in short.items():
        token(key)
        token(value)
    regions = object_value(document.get("regions"))
    if not 1 <= len(regions) <= MAX_REGIONS:
        raise EvidenceError("region_limit_exceeded")
    zones = {}
    for region, metadata in regions.items():
        if not isinstance(region, str) or not REGION.fullmatch(region):
            raise EvidenceError("invalid_catalog_region")
        zones[region] = token(object_value(metadata).get("dataZone"))

    # Reuse the existing naming authority, not its planning, inference or write paths.
    spec = importlib.util.spec_from_file_location("capacity_naming", ROOT / "scripts" / "sync-model-capacity.py")
    if spec is None or spec.loader is None:
        raise EvidenceError("catalog_naming_unavailable")
    naming_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(naming_module)
    deployments = []
    identities = set()
    for entry in array(document.get("catalog"), MAX_MODELS):
        entry = object_value(entry)
        model_name, model_format = token(entry.get("name")), provider_format(entry.get("format"))
        for item in array(entry.get("deployments"), MAX_DEPLOYMENTS):
            item = object_value(item)
            region, sku, version = token(item.get("region")), token(item.get("sku")), token(item.get("version"))
            if region not in zones or sku not in short:
                raise EvidenceError("invalid_catalog_membership")
            try:
                name = token(naming_module.deployment_name(document, model_name, item))
            except (KeyError, IndexError, AttributeError, ValueError):
                raise EvidenceError("invalid_catalog_naming") from None
            if (region, name.casefold()) in identities:
                raise EvidenceError("duplicate_catalog_deployment")
            identities.add((region, name.casefold()))
            pool = item.get("maxCapacityPool")
            if pool is not None and (
                not isinstance(pool, str)
                or not re.fullmatch(r"(?:global|region:[a-z0-9]{1,32}|data-zone:[A-Za-z0-9_-]{1,32})", pool)
            ):
                raise EvidenceError("invalid_catalog_pool")
            maximum = number(item["maxCapacity"]) if "maxCapacity" in item else None
            deployments.append(Deployment(
                name, region, Model(model_format, model_name, version, sku),
                number(item.get("capacity")), maximum, pool,
            ))
    if not deployments or len(deployments) > MAX_DEPLOYMENTS:
        raise EvidenceError("deployment_limit_exceeded")
    if len({(d.model.format, d.model.name, d.model.version) for d in deployments}) > MAX_MODELS:
        raise EvidenceError("model_limit_exceeded")
    return Catalog(zones, foundry_token, tuple(deployments), digest)


@dataclass(frozen=True)
class Scope:
    subscription: str
    resource_group: str
    environment: str
    catalog: Catalog

    def __post_init__(self) -> None:
        subscription_id(self.subscription)
        token(self.resource_group)
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,18}[a-z0-9])", self.environment):
            raise EvidenceError("invalid_environment")

    @property
    def group_path(self) -> str:
        return f"/subscriptions/{self.subscription}/resourceGroups/{self.resource_group}"

    def account_path(self, name: str) -> str:
        return f"{self.group_path}/providers/{NAMESPACE}/{token(name)}"

    def public(self) -> dict:
        return {
            "subscriptionFingerprint": hashlib.sha256(self.subscription.lower().encode()).hexdigest(),
            "resourceGroup": self.resource_group, "environment": self.environment,
            "regions": sorted(self.catalog.regions),
        }


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def create(cls, days: int, end: str | None, now: datetime) -> Window:
        if type(days) is not int or not 1 <= days <= 7:
            raise EvidenceError("invalid_window_days")
        latest = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        finish = parse_utc(end) if end is not None else latest
        if (
            finish.minute or finish.second or finish.microsecond
            or not latest - timedelta(hours=24) <= finish <= latest
        ):
            raise EvidenceError("invalid_window_end")
        return cls(finish - timedelta(days=days), finish)

    @property
    def hours(self) -> int:
        return int((self.end - self.start).total_seconds() // 3600)

    @property
    def timespan(self) -> str:
        return f"{utc_text(self.start)}/{utc_text(self.end)}"

    def public(self) -> dict:
        return {
            "start": utc_text(self.start), "endExclusive": utc_text(self.end),
            "interval": "PT1H", "aggregation": "Total", "expectedBucketsPerSeries": self.hours,
        }


def az_command() -> list[str]:
    executable = shutil.which("az")
    if executable is None:
        raise EvidenceError("azure_cli_unavailable")
    if os.name == "nt":
        # The official MSI launcher otherwise leaves a Python child behind when
        # a timed-out az.cmd is killed. Invoke that same bundled interpreter.
        interpreter = Path(executable).resolve().parent.parent / "python.exe"
        if Path(executable).suffix.lower() != ".cmd" or not interpreter.is_file():
            raise EvidenceError("unsupported_azure_cli_launcher")
        return [str(interpreter), "-IBm", "azure.cli"]
    return [executable]


@dataclass(frozen=True)
class ProcessResult:
    body: bytes
    warning: bool


def run_bounded(command: list[str], timeout: float, limit: int) -> ProcessResult:
    """Drain both pipes within byte and wall-clock budgets; never publish stderr."""
    environment = {
        **os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
        "AZURE_CORE_COLLECT_TELEMETRY": "no", "AZURE_LOGGING_ENABLE_LOG_FILE": "no",
    }
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env=environment, start_new_session=os.name != "nt",
        )
    except OSError:
        raise EvidenceError("azure_cli_unavailable") from None
    packets: queue.Queue = queue.Queue(maxsize=16)
    stopped = threading.Event()
    received = {"out": 0, "err": 0}

    def drain(stream, label: str) -> None:
        maximum = limit if label == "out" else 64 * 1024
        try:
            while not stopped.is_set():
                chunk = stream.read1(min(8192, maximum + 1 - received[label]))
                received[label] += len(chunk)
                overflow = received[label] > maximum
                packet = (label + "_overflow", b"") if overflow else (label, chunk)
                while not stopped.is_set():
                    try:
                        packets.put(packet, timeout=0.05)
                        break
                    except queue.Full:
                        continue
                if not chunk or overflow:
                    return
        except OSError:
            while not stopped.is_set():
                try:
                    packets.put(("pipe_error", b""), timeout=0.05)
                    return
                except queue.Full:
                    continue

    threads = [
        threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    output = bytearray()
    stderr_bytes = 0
    finished = set()
    failure = None
    try:
        while len(finished) < 2 or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EvidenceError("source_deadline_exceeded")
            try:
                label, chunk = packets.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if label == "pipe_error":
                raise EvidenceError("source_pipe_unavailable")
            if label == "out_overflow":
                raise EvidenceError("response_too_large")
            if label == "err_overflow":
                raise EvidenceError("diagnostics_too_large")
            if not chunk:
                finished.add(label)
            elif label == "out":
                if len(output) + len(chunk) > limit:
                    raise EvidenceError("response_too_large")
                output.extend(chunk)
            else:
                stderr_bytes += len(chunk)
                if stderr_bytes > 64 * 1024:
                    raise EvidenceError("diagnostics_too_large")
        if process.returncode != 0:
            raise EvidenceError("azure_read_failed")
        return ProcessResult(bytes(output), bool(stderr_bytes))
    except EvidenceError as exc:
        failure = exc
        raise
    finally:
        stopped.set()
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=1)
        if failure is not None:
            failure.response_bytes = min(limit, received["out"])
        process.stdout.close()
        process.stderr.close()


@dataclass(frozen=True)
class ReadResult:
    document: object
    warning: bool
    byte_count: int | None
    codes: tuple[str, ...] = ()
    pages: tuple[dict, ...] = ()


# Projection happens inside az as well as at the parser/report boundary. Account
# keys, endpoints, owners, arbitrary tags, metric descriptions and logs never leave it.
PROJECTIONS = {
    "group": '{id:id,name:name,env:tags.env,azdEnv:tags."azd-env-name",managedBy:tags.managedBy}',
    "accounts": '{nextLink:nextLink,value:value[].{id:id,name:name,kind:kind,location:location,'
                'env:tags.env,azdEnv:tags."azd-env-name",managedBy:tags.managedBy}}',
    "deployments": "{nextLink:nextLink,value:value[].{id:id,name:name,sku:sku,"
                   "properties:{model:properties.model,provisioningState:properties.provisioningState}}}",
    "quota": "{nextLink:nextLink,value:value[].{name:{value:name.value},"
             "currentValue:currentValue,limit:limit,unit:unit}}",
    "availability": "{nextLink:nextLink,value:value[].{location:location,properties:{"
                    "model:properties.model,skuName:properties.skuName,availableCapacity:properties.availableCapacity}}}",
    "definitions": "{nextLink:nextLink,value:value[].{name:{value:name.value},unit:unit,"
                   "dimensions:dimensions[].{value:value},supportedAggregationTypes:supportedAggregationTypes,"
                   "primaryAggregationType:primaryAggregationType,"
                   "metricAvailabilities:metricAvailabilities[].{timeGrain:timeGrain}}}",
    "metrics": "{nextLink:nextLink,timespan:timespan,interval:interval,namespace:namespace,"
               "resourceregion:resourceregion,value:value[].{id:id,name:{value:name.value},"
               "unit:unit,errorCode:errorCode,hasErrorMessage:!!errorMessage,"
               "timeseries:timeseries[].{dimensionCount:length(metadatavalues || `[]`),"
               "metadatavalues:metadatavalues[?contains(`"
               + json.dumps(list(DIMENSIONS), separators=(",", ":")) +
               "`, name.value)].{name:{value:name.value},value:value},"
               "data:data[].{timeStamp:timeStamp,total:total}}}}",
}


class AzureReader:
    def __init__(self, scope: Scope, runner: Callable = run_bounded, clock: Callable = time.monotonic):
        self.scope = scope
        self.runner = runner
        self.clock = clock
        self.deadline = clock() + COLLECTION_SECONDS
        self.calls = 0
        self.bytes = 0

    def read(
        self, operation: str, *, account: str | None = None, region: str | None = None,
        model: Model | None = None, window: Window | None = None, metrics: tuple[str, ...] = (),
    ) -> ReadResult:
        if operation not in PROJECTIONS:
            raise EvidenceError("unapproved_read_operation")
        scope = self.scope
        parameters = {"api-version": COGNITIVE_API}
        if operation == "group":
            path = scope.group_path
            parameters["api-version"] = "2024-03-01"
        elif operation == "accounts":
            return self._accounts()
        elif operation == "quota":
            if region not in scope.catalog.regions:
                raise EvidenceError("unapproved_read_region")
            path = f"/subscriptions/{scope.subscription}/providers/Microsoft.CognitiveServices/locations/{region}/usages"
        elif operation == "availability":
            if model not in {item.model for item in scope.catalog.deployments}:
                raise EvidenceError("unapproved_read_model")
            path = f"/subscriptions/{scope.subscription}/providers/Microsoft.CognitiveServices/modelCapacities"
            parameters.update(modelFormat=model.format, modelName=model.name, modelVersion=model.version)
        else:
            if region not in scope.catalog.regions or account is None or not owned_account_name(scope, region, account):
                raise EvidenceError("unapproved_read_account")
            path = scope.account_path(account)
            if operation == "deployments":
                path += "/deployments"
            elif operation == "definitions":
                path += "/providers/Microsoft.Insights/metricDefinitions"
                parameters.update({"api-version": DEFINITIONS_API, "metricnamespace": NAMESPACE})
            else:
                if window is None or not metrics or set(metrics) - set(METRICS) or len(metrics) != len(set(metrics)):
                    raise EvidenceError("unapproved_metric_query")
                if not 24 <= window.hours <= MAX_POINTS or window.start >= window.end:
                    raise EvidenceError("invalid_metric_window")
                path += "/providers/Microsoft.Insights/metrics"
                parameters.update({
                    "api-version": METRICS_API, "metricnamespace": NAMESPACE,
                    "metricnames": ",".join(metrics), "timespan": window.timespan,
                    "interval": "PT1H", "aggregation": "Total", "top": str(MAX_SERIES + 1),
                    "$filter": " and ".join(f"{name} eq '*'" for name in DIMENSIONS),
                    "AutoAdjustTimegrain": "false", "ValidateDimensions": "true",
                })
        return self._read_page(operation, path, parameters)

    def _read_page(self, operation: str, path: str, parameters: dict[str, str]) -> ReadResult:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise EvidenceError("collection_deadline_exceeded")
        if self.calls >= MAX_CALLS:
            raise EvidenceError("read_limit_exceeded")
        byte_limit = min(MAX_RESPONSE_BYTES, MAX_TOTAL_RESPONSE_BYTES - self.bytes)
        if byte_limit <= 0:
            raise EvidenceError("response_budget_exceeded")
        self.calls += 1
        command = [
            *az_command(), "rest", "--method", "GET", "--url", f"{ARM}{path}?{urlencode(parameters)}",
            "--subscription", self.scope.subscription, "--only-show-errors", "--output", "json",
            "--query", PROJECTIONS[operation],
        ]
        try:
            result = self.runner(command, min(SOURCE_SECONDS, remaining), byte_limit)
        except EvidenceError as exc:
            # Unknown failure accounting reserves the allowance rather than
            # silently granting the next call a fresh budget.
            self.bytes += byte_limit if exc.response_bytes is None else min(byte_limit, exc.response_bytes)
            raise
        if len(result.body) > byte_limit:
            self.bytes += byte_limit
            raise EvidenceError("response_too_large", response_bytes=byte_limit)
        self.bytes += len(result.body)
        try:
            document = strict_json(result.body)
        except EvidenceError as exc:
            exc.response_bytes = len(result.body)
            raise
        return ReadResult(document, result.warning, len(result.body))

    def _accounts(self) -> ReadResult:
        path = f"{self.scope.group_path}/providers/{NAMESPACE}"
        parameters = {"api-version": COGNITIVE_API}
        combined: list[dict] = []
        pages: list[dict] = []
        cursors: set[str] = set()
        byte_count: int | None = 0
        warning = False

        def incomplete(code: str) -> ReadResult:
            return ReadResult(None, warning, byte_count, (code,), tuple(pages))

        while True:
            page = {
                "page": len(pages) + 1, "startedAt": utc_text(datetime.now(UTC)), "finishedAt": None,
                "status": "available", "codes": [], "rowCount": None, "accounts": [],
                "responseBytes": None, "attempted": False,
            }
            pages.append(page)
            result = None
            calls_before = self.calls
            try:
                result = self._read_page("accounts", path, parameters)
                page["responseBytes"] = result.byte_count
                byte_count = byte_count + result.byte_count if byte_count is not None and result.byte_count is not None else None
                warning = warning or result.warning
                document = object_value(result.document)
                items = rows(document, MAX_ACCOUNTS)
                # The existing identity/unique-name contract also makes IDs
                # unique and bounds the total rows across all pages.
                candidates = parse_accounts({"value": [*combined, *items]}, self.scope)
                combined.extend(items)
                page_names = {item["name"] for item in items}
                page["rowCount"] = len(items)
                page["accounts"] = [
                    {"region": region, "name": name}
                    for region, name in sorted(candidates.items()) if name in page_names
                ]
                if result.warning:
                    page["status"] = "partial"
                    page["codes"] = ["azure_cli_warning"]
            except EvidenceError as exc:
                if result is None:
                    received = exc.response_bytes if self.calls > calls_before else 0
                    page["responseBytes"] = received
                    byte_count = byte_count + received if byte_count is not None and received is not None else None
                page["status"] = "unavailable"
                page["codes"] = [exc.code]
                return incomplete(exc.code)
            finally:
                page["attempted"] = self.calls > calls_before
                page["finishedAt"] = utc_text(datetime.now(UTC))
            next_link = document.get("nextLink")
            if next_link is None or next_link == "":
                return ReadResult({"value": combined}, warning, byte_count, pages=tuple(pages))
            try:
                parameters = account_continuation(next_link, self.scope)
            except EvidenceError as exc:
                return incomplete(exc.code)
            cursor = parameters["$skiptoken"]
            if cursor in cursors:
                return incomplete("repeated_account_cursor")
            cursors.add(cursor)
            if len(pages) >= MAX_ACCOUNT_PAGES:
                return incomplete("account_page_limit_exceeded")


def account_continuation(link: object, scope: Scope) -> dict[str, str]:
    """Accept only the observed account-list continuation contract, never its URL."""
    if (
        not isinstance(link, str) or len(link) > MAX_ACCOUNT_LINK_BYTES
        or not re.fullmatch(r"[\x21-\x7e]+", link)
        or "#" in link
        or re.search(r"%(?![0-9A-Fa-f]{2})", link)
    ):
        raise EvidenceError("invalid_account_continuation")
    try:
        parsed = urlsplit(link)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=2)
    except ValueError:
        raise EvidenceError("invalid_account_continuation") from None
    expected_path = f"{scope.group_path}/providers/{NAMESPACE}"
    if (
        parsed.scheme != "https" or parsed.netloc.casefold() != "management.azure.com"
        or parsed.path.casefold() != expected_path.casefold() or parsed.fragment
    ):
        raise EvidenceError("unapproved_account_continuation")
    parameters = dict(pairs)
    if (
        len(pairs) != 2 or set(parameters) != {"api-version", "$skiptoken"}
        or parameters["api-version"] != COGNITIVE_API
    ):
        raise EvidenceError("unapproved_account_continuation_query")
    cursor = parameters["$skiptoken"]
    if (
        not cursor or len(cursor) > MAX_ACCOUNT_CURSOR_BYTES
        or not re.fullmatch(r"[\x20-\x7e]+", cursor)
    ):
        raise EvidenceError("invalid_account_cursor")
    # The caller rebuilds the exact known host/path using these two parameters.
    # No server URL, cursor value or subscription-bearing ID enters the report.
    return parameters


def owned_account_name(scope: Scope, region: str, name: object) -> bool:
    if not isinstance(name, str):
        return False
    prefix = f"mf-{scope.catalog.foundry_token}-{scope.environment}-{region}-"
    return len(prefix) + 13 <= 60 and re.fullmatch(re.escape(prefix) + r"[a-z0-9]{13}", name) is not None


def owned_tags(row: dict, scope: Scope) -> bool:
    return row.get("env") == scope.environment and row.get("azdEnv") == scope.environment and row.get("managedBy") == "azd-bicep"


def parse_group(document: object, scope: Scope) -> dict:
    row = object_value(document)
    if (
        not isinstance(row.get("name"), str) or row["name"].casefold() != scope.resource_group.casefold()
        or not isinstance(row.get("id"), str) or row["id"].casefold() != scope.group_path.casefold()
        or not owned_tags(row, scope)
    ):
        raise EvidenceError("resource_group_ownership_mismatch")
    return {"verified": True}


def parse_accounts(document: object, scope: Scope) -> dict[str, str]:
    found = {}
    suffixes = set()
    names = set()
    for row in rows(document, MAX_ACCOUNTS):
        row = object_value(row)
        name = token(row.get("name"))
        if (
            not isinstance(row.get("id"), str)
            or row["id"].casefold() != scope.account_path(name).casefold()
            or name.casefold() in names
        ):
            raise EvidenceError("account_inventory_scope_mismatch")
        names.add(name.casefold())
        region = row.get("location")
        if not isinstance(region, str):
            raise EvidenceError("invalid_account_region")
        candidates = [r for r in scope.catalog.regions if owned_account_name(scope, r, name)]
        if not candidates:
            continue
        if (
            region not in candidates or row.get("kind") != "AIServices" or not owned_tags(row, scope)
        ):
            raise EvidenceError("account_ownership_mismatch")
        if region in found:
            raise EvidenceError("ambiguous_account_inventory")
        found[region] = name
        suffixes.add(name[-13:])
    if len(suffixes) > 1:
        raise EvidenceError("account_suffix_mismatch")
    return found


def parse_deployments(document: object, scope: Scope, account: str) -> list[dict]:
    result = []
    seen = set()
    for row in rows(document, MAX_DEPLOYMENTS):
        row = object_value(row)
        name = token(row.get("name"))
        expected_id = f"{scope.account_path(account)}/deployments/{name}"
        if (
            not isinstance(row.get("id"), str) or row["id"].casefold() != expected_id.casefold()
            or name.casefold() in seen
        ):
            raise EvidenceError("deployment_inventory_scope_mismatch")
        seen.add(name.casefold())
        properties = object_value(row.get("properties"))
        model = object_value(properties.get("model"))
        sku = object_value(row.get("sku"))
        result.append({
            "name": name,
            "model": Model(provider_format(model.get("format")), token(model.get("name")), token(model.get("version")), token(sku.get("name"))),
            "capacity": number(sku.get("capacity")),
            "state": token(properties.get("provisioningState")),
        })
    return result


def parse_quota(document: object) -> list[dict]:
    result = []
    seen = set()
    for row in rows(document):
        row = object_value(row)
        counter = token(object_value(row.get("name")).get("value"))
        unit = token(row.get("unit"))
        if counter.casefold() in seen:
            raise EvidenceError("duplicate_quota_counter")
        seen.add(counter.casefold())
        result.append({
            "counter": counter, "unit": unit,
            "currentValue": number(row.get("currentValue")), "limit": number(row.get("limit")),
        })
    return result


def parse_availability(document: object, model: Model, catalog: Catalog) -> list[dict]:
    result = []
    seen = set()
    for row in rows(document):
        row = object_value(row)
        properties = object_value(row.get("properties"))
        identity = object_value(properties.get("model"))
        if (identity.get("format"), identity.get("name"), identity.get("version")) != (model.format, model.name, model.version):
            raise EvidenceError("availability_identity_mismatch")
        region, sku = token(row.get("location")), token(properties.get("skuName"))
        if (region, sku) in seen:
            raise EvidenceError("duplicate_availability")
        seen.add((region, sku))
        capacity = number(properties.get("availableCapacity"))
        if region in catalog.regions:
            result.append({
                "region": region, "sku": sku, "availableCapacity": capacity,
                "model": {"format": model.format, "name": model.name, "version": model.version},
                "unit": "raw_capacity_units",
            })
    return result


def parse_definitions(document: object) -> dict[str, str | None]:
    result = dict.fromkeys(METRICS, "metric_definition_missing")
    seen = set()
    for row in rows(document):
        row = object_value(row)
        name = object_value(row.get("name")).get("value")
        if name not in METRICS:
            continue
        if name in seen:
            raise EvidenceError("duplicate_metric_definition")
        seen.add(name)
        dimensions = [token(object_value(d).get("value")) for d in array(row.get("dimensions"), 64)]
        grains = [token(object_value(g).get("timeGrain")) for g in array(row.get("metricAvailabilities"), 32)]
        aggregations = array(row.get("supportedAggregationTypes"), 16)
        result[name] = None if (
            row.get("unit") == "Count" and "Total" in aggregations
            and "PT1H" in grains and set(DIMENSIONS).issubset(dimensions)
            and len(dimensions) == len(set(dimensions))
        ) else "unsupported_metric_definition"
    return result


def unknown_usage(code: str) -> dict:
    return {
        "status": "unknown", "codes": [code], "unit": "Count", "total": None,
        "observedTotal": None, "observedPeakHourlyCount": None, "series": 0,
        "samples": 0, "zeroSamples": 0, "expectedSamples": None,
        "firstSampleAt": None, "lastSampleAt": None,
        "coverage": "returned_series_only",
    }


@dataclass
class PointBudget:
    remaining: int = MAX_TOTAL_POINTS

    def consume(self, count: int) -> None:
        if count > self.remaining:
            raise EvidenceError("point_budget_exceeded")
        self.remaining -= count


def parse_metrics(
    document: object, scope: Scope, region: str, account: str, live: dict[str, dict],
    window: Window, requested: tuple[str, ...], budget: PointBudget,
) -> tuple[dict[str, dict], list[str], int]:
    document = object_value(document)
    timespan = document.get("timespan")
    if not isinstance(timespan, str) or timespan.count("/") != 1:
        raise EvidenceError("invalid_metric_window")
    start, end = (parse_utc(part) for part in timespan.split("/"))
    if (
        (start, end) != (window.start, window.end) or document.get("interval") != "PT1H"
        or document.get("namespace") not in (None, NAMESPACE)
        or document.get("resourceregion") not in (None, region)
    ):
        raise EvidenceError("metric_window_or_scope_mismatch")
    by_metric: dict[str, list[dict]] = defaultdict(list)
    for row in rows(document, len(METRICS)):
        row = object_value(row)
        name = object_value(row.get("name")).get("value")
        if not isinstance(name, str) or name not in requested:
            raise EvidenceError("unexpected_metric")
        by_metric[name].append(row)
    result = {name: {} for name in live}
    source_codes = set()
    excluded = 0
    for metric in requested:
        entries = by_metric[metric]
        error = "metric_missing" if not entries else "duplicate_metric" if len(entries) != 1 else None
        if error is None:
            entry = entries[0]
            expected_id = f"{scope.account_path(account)}/providers/Microsoft.Insights/metrics/{metric}"
            if (
                not isinstance(entry.get("id"), str) or entry["id"].casefold() != expected_id.casefold()
                or entry.get("unit") != "Count"
            ):
                error = "metric_identity_or_unit_mismatch"
            elif entry.get("errorCode") not in (None, "Success", "0") or entry.get("hasErrorMessage"):
                error = "metric_error"
            elif not isinstance(entry.get("timeseries"), list):
                error = "invalid_metric_series"
            elif len(entry["timeseries"]) > MAX_SERIES:
                error = "series_limit_exceeded"
        if error:
            source_codes.add(error)
            for name in live:
                result[name][metric] = unknown_usage(error)
            continue
        accumulators = {
            name: {"buckets": defaultdict(int), "samples": 0, "zeros": 0, "series": 0, "codes": set()}
            for name in live
        }
        seen_series = set()
        metric_codes = set()
        for series in entry["timeseries"]:
            name = None
            try:
                series = object_value(series)
                metadata = array(series.get("metadatavalues"), len(DIMENSIONS))
                if series.get("dimensionCount") != len(DIMENSIONS) or len(metadata) != len(DIMENSIONS):
                    raise EvidenceError("metric_dimensions_mismatch")
                dimensions = {}
                for item in metadata:
                    item = object_value(item)
                    dimension = object_value(item.get("name")).get("value")
                    if not isinstance(dimension, str) or dimension not in DIMENSIONS or dimension in dimensions:
                        raise EvidenceError("metric_dimensions_mismatch")
                    dimensions[dimension] = token(item.get("value"))
                name = dimensions["ModelDeploymentName"]
                points = array(series.get("data"), MAX_POINTS)
                budget.consume(len(points))
                series_key = tuple(dimensions[d] for d in DIMENSIONS)
                if series_key in seen_series:
                    raise EvidenceError("duplicate_metric_series")
                seen_series.add(series_key)
                if name not in live:
                    excluded += 1
                    continue
                accumulator = accumulators[name]
                model = live[name]["model"]
                if (dimensions["ModelName"], dimensions["ModelVersion"]) != (model.name, model.version):
                    raise EvidenceError("metric_model_version_mismatch")
                accumulator["series"] += 1
                samples = {}
                for point in points:
                    point = object_value(point)
                    stamp = parse_utc(point.get("timeStamp"))
                    if (
                        stamp.minute or stamp.second or stamp.microsecond
                        or not window.start <= stamp < window.end
                    ):
                        raise EvidenceError("metric_sample_out_of_window")
                    if stamp in samples:
                        raise EvidenceError("duplicate_metric_sample")
                    samples[stamp] = number(point["total"]) if point.get("total") is not None else None
                valid = {stamp: total for stamp, total in samples.items() if total is not None}
                if len(valid) != window.hours:
                    accumulator["codes"].add("missing_metric_samples")
                for stamp, total in valid.items():
                    accumulator["buckets"][stamp] = number(accumulator["buckets"][stamp] + total)
                    accumulator["samples"] += 1
                    accumulator["zeros"] += int(total == 0)
            except EvidenceError as exc:
                if exc.code == "point_budget_exceeded":
                    raise
                if name in accumulators:
                    accumulators[name]["codes"].add(exc.code)
                else:
                    metric_codes.add(exc.code)
        source_codes.update(metric_codes)
        for name, accumulator in accumulators.items():
            codes = accumulator["codes"] | metric_codes
            count = accumulator["samples"]
            series_count = accumulator["series"]
            if not series_count:
                codes.add("no_series")
            elif not count:
                codes.add("no_samples")
            buckets = accumulator["buckets"]
            total = number(sum(buckets.values())) if count else None
            source_codes.update(codes)
            result[name][metric] = {
                "status": "measured" if count and not codes else "partial" if count else "unknown",
                "codes": sorted(codes), "unit": "Count",
                "total": total if not codes else None, "observedTotal": total,
                "observedPeakHourlyCount": max(buckets.values()) if count else None,
                "series": series_count, "samples": count, "zeroSamples": accumulator["zeros"],
                "expectedSamples": series_count * window.hours if series_count else None,
                "firstSampleAt": utc_text(min(buckets)) if count else None,
                "lastSampleAt": utc_text(max(buckets)) if count else None,
                "coverage": "returned_series_only",
            }
    return result, sorted(source_codes), excluded


def scope_regions(scope_name: str, catalog: Catalog) -> set[str]:
    if scope_name == "global":
        return set(catalog.regions)
    if scope_name.startswith("region:") and scope_name[7:] in catalog.regions:
        return {scope_name[7:]}
    if scope_name.startswith("data-zone:"):
        result = {r for r, zone in catalog.regions.items() if zone == scope_name[10:]}
        if result:
            return result
    raise EvidenceError("invalid_pool_scope")


def parse_pool_evidence(document: object, scope: Scope, now: datetime) -> dict:
    document = object_value(document)
    if set(document) != {"schemaVersion", "subscriptionId", "observedAt", "reference", "pools"}:
        raise EvidenceError("invalid_pool_evidence")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise EvidenceError("invalid_pool_evidence_version")
    if subscription_id(document["subscriptionId"]) != scope.subscription.lower():
        raise EvidenceError("pool_subscription_mismatch")
    observed = parse_utc(document["observedAt"])
    if not now - timedelta(hours=24) <= observed <= now:
        raise EvidenceError("stale_pool_evidence")
    reference = token(document["reference"])
    pools = array(document["pools"], MAX_MODELS)
    if not pools:
        raise EvidenceError("empty_pool_evidence")
    parsed = []
    counter_memberships = set()
    model_memberships = set()
    for pool in pools:
        pool = object_value(pool)
        if set(pool) != {"counter", "unit", "scope", "regions", "model", "capacityUnitsPerCounterUnit"}:
            raise EvidenceError("invalid_pool_evidence")
        model = object_value(pool["model"])
        if set(model) != {"format", "name", "sku", "versions"}:
            raise EvidenceError("invalid_pool_model")
        format_name, name, sku = provider_format(model["format"]), token(model["name"]), token(model["sku"])
        versions = [token(v) for v in array(model["versions"], MAX_MODELS)]
        if not versions or len(versions) != len(set(versions)):
            raise EvidenceError("invalid_pool_versions")
        scope_name = pool["scope"]
        if not isinstance(scope_name, str):
            raise EvidenceError("invalid_pool_scope")
        expected_regions = scope_regions(scope_name, scope.catalog)
        regions = [token(r) for r in array(pool["regions"], MAX_REGIONS)]
        if len(regions) != len(set(regions)) or set(regions) != expected_regions:
            raise EvidenceError("incomplete_pool_membership")
        counter, unit = token(pool["counter"]), token(pool["unit"])
        if number(pool["capacityUnitsPerCounterUnit"]) != 1:
            raise EvidenceError("unsupported_unit_conversion")
        expected = [
            d for d in scope.catalog.deployments
            if d.region in expected_regions and (d.model.format, d.model.name, d.model.sku) == (format_name, name, sku)
        ]
        if not expected or {d.model.version for d in expected} != set(versions):
            raise EvidenceError("pool_version_membership_mismatch")
        for region in regions:
            # A counter covers versions together. Distinct unit or version labels
            # must not turn its one allocation into several independent pools.
            counter_key = (counter.casefold(), region)
            model_key = (format_name, name, sku, region)
            if counter_key in counter_memberships or model_key in model_memberships:
                raise EvidenceError("overlapping_pool_membership")
            counter_memberships.add(counter_key)
            model_memberships.add(model_key)
        parsed.append({
            "counter": counter, "unit": unit, "scope": scope_name, "regions": sorted(regions),
            "model": {"format": format_name, "name": name, "sku": sku, "versions": sorted(versions)},
            "capacityUnitsPerCounterUnit": 1,
        })
    return {"observedAt": utc_text(observed), "reference": reference, "pools": parsed}


def counter_groups(quotas: dict[str, list[dict]], sources: dict[str, dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for region, items in sorted(quotas.items()):
        source = sources[f"quota:{region}"]
        for item in items:
            key = (item["counter"], item["unit"])
            group = groups.setdefault(key, {
                "counter": item["counter"], "unit": item["unit"], "scope": "unknown",
                "headroom": None, "observations": [],
            })
            group["observations"].append({
                "region": region, "currentValue": item["currentValue"], "limit": item["limit"],
                "source": source["id"], "retrievedAt": source["finishedAt"], "sourceStatus": source["status"],
            })
    return [groups[key] for key in sorted(groups)]


def pool_rollups(
    evidence: dict | None, catalog: Catalog, inventories: dict[str, list[dict]],
    quotas: dict[str, list[dict]], availability: dict[tuple[str, str, str], list[dict]],
    sources: dict[str, dict], evidence_error: str | None,
) -> tuple[list[dict], dict]:
    pools = []
    mapped = set()
    for assertion in evidence["pools"] if evidence is not None else []:
        identity = assertion["model"]
        regions = assertion["regions"]
        expected = [
            d for d in catalog.deployments if d.region in regions
            and (d.model.format, d.model.name, d.model.sku) == (identity["format"], identity["name"], identity["sku"])
        ]
        codes = set()
        observations = []
        catalog_allocation = 0
        known_allocation = 0
        platforms = []
        for source_id in ["group", "accounts", *(f"deployments:{r}" for r in regions), *(f"quota:{r}" for r in regions)]:
            if sources.get(source_id, {}).get("status") != "available":
                codes.add("incomplete_pool_sources")
        for region in regions:
            matching = [row for row in quotas.get(region, []) if row["counter"] == assertion["counter"]]
            if len(matching) != 1 or matching[0]["unit"] != assertion["unit"]:
                codes.add("pool_counter_or_unit_mismatch")
            else:
                observations.append(matching[0])
        values = {(row["currentValue"], row["limit"]) for row in observations}
        current, limit = next(iter(values)) if len(values) == 1 else (None, None)
        if len(values) > 1:
            codes.add("contradictory_pool_counters")
        for deployment in expected:
            mapped.add((deployment.region, deployment.name))
            live = [row for row in inventories.get(deployment.region, []) if row["name"] == deployment.name]
            if len(live) != 1:
                codes.add("missing_pool_deployment")
                continue
            row = live[0]
            if row["model"] != deployment.model or row["state"] != "Succeeded":
                codes.add("pool_deployment_identity_or_state_mismatch")
                continue
            catalog_allocation = number(catalog_allocation + row["capacity"])
        for region in regions:
            for row in inventories.get(region, []):
                model = row["model"]
                if (model.format, model.name, model.sku) != (identity["format"], identity["name"], identity["sku"]):
                    continue
                if row["state"] != "Succeeded" or model.version not in identity["versions"]:
                    codes.add("unsettled_or_unreviewed_pool_deployment")
                    continue
                known_allocation = number(known_allocation + row["capacity"])
        for version in identity["versions"]:
            key = (identity["format"], identity["name"], version)
            source_id = availability_source_id(key)
            if sources.get(source_id, {}).get("status") != "available":
                codes.add("incomplete_pool_sources")
            for region in regions:
                matches = [row for row in availability.get(key, []) if row["region"] == region and row["sku"] == identity["sku"]]
                if len(matches) != 1:
                    codes.add("missing_pool_availability")
                else:
                    platforms.append({"region": region, "version": version, "availableCapacity": matches[0]["availableCapacity"]})
        if current is not None and limit is not None:
            if current > limit or known_allocation > current:
                codes.add("contradictory_pool_allocation")
            if any(row["availableCapacity"] > limit - current for row in platforms):
                codes.add("contradictory_pool_availability")
        else:
            codes.add("missing_pool_counter")
        pools.append({
            **assertion, "authority": "operator_asserted", "status": "unknown" if codes else "consistent",
            "codes": sorted(codes), "counterCurrentValue": current, "counterLimit": limit,
            "catalogAllocation": catalog_allocation if not codes else None,
            "knownUncataloguedAllocation": known_allocation - catalog_allocation if not codes else None,
            "outsideCatalogOrUnattributedAllocation": current - catalog_allocation if not codes else None,
            "headroom": limit - current if not codes else None,
            "platformAvailability": platforms, "deployableHeadroom": None,
            "reference": evidence["reference"], "assertedAt": evidence["observedAt"],
        })
    unmapped = [
        {"region": d.region, "name": d.name} for d in catalog.deployments if (d.region, d.name) not in mapped
    ]
    codes = set()
    if evidence_error:
        codes.add(evidence_error)
    if evidence is None:
        codes.add("pool_scope_not_established")
    if unmapped:
        codes.add("unmapped_catalog_deployments")
    if any(row["status"] != "consistent" for row in pools):
        codes.add("incomplete_pool_evidence")
    return pools, {"status": "unknown" if codes else "consistent", "codes": sorted(codes), "unmappedDeployments": unmapped}


def availability_source_id(key: tuple[str, str, str]) -> str:
    return "availability:" + ":".join(key)


def collect(
    scope: Scope, window: Window, reader: AzureReader, pool_path: Path | None = None,
    now: Callable = lambda: datetime.now(UTC),
) -> dict:
    started = now()
    sources: dict[str, dict] = {}
    inventories: dict[str, list[dict]] = {}
    quotas: dict[str, list[dict]] = {}
    availability: dict[tuple[str, str, str], list[dict]] = {}
    usage: dict[tuple[str, str], dict] = {}
    catalog = scope.catalog
    point_budget = PointBudget(MAX_TOTAL_POINTS)
    account_names: dict[str, str] = {}
    excluded_series = 0

    def skip(operation: str, source_id: str, code: str, target: dict) -> None:
        sources[source_id] = {
            "id": source_id, "operation": operation, "target": target, "attempted": False,
            "startedAt": None, "finishedAt": utc_text(now()), "status": "unavailable",
            "codes": [code], "responseBytes": 0, "rowCount": None,
        }

    def capture(operation: str, source_id: str, target: dict, parser: Callable, **arguments):
        source = {
            "id": source_id, "operation": operation, "target": target, "attempted": True,
            "startedAt": utc_text(now()), "finishedAt": None, "status": "available",
            "codes": [], "responseBytes": None, "rowCount": None,
        }
        sources[source_id] = source
        try:
            result = reader.read(operation, **arguments)
            source["responseBytes"] = result.byte_count
            if result.pages:
                source["pages"] = list(result.pages)
            source["codes"].extend(result.codes)
            if result.warning:
                source["codes"].append("azure_cli_warning")
            if result.document is None and result.codes:
                source["status"] = "partial" if any(p["status"] != "unavailable" for p in result.pages) else "unavailable"
                return None
            document = object_value(result.document)
            if document.get("nextLink"):
                source["codes"].append("pagination_not_followed")
            parsed = parser(document)
            source["rowCount"] = len(parsed) if isinstance(parsed, (list, dict)) else None
            if source["codes"]:
                source["status"] = "partial"
            return parsed
        except EvidenceError as exc:
            source["status"] = "unavailable"
            if exc.response_bytes is not None:
                source["responseBytes"] = exc.response_bytes
            source["codes"] = sorted({*source["codes"], exc.code})
            return None
        finally:
            source["finishedAt"] = utc_text(now())

    capture("group", "group", {}, lambda raw: parse_group(raw, scope))
    group_verified = sources["group"]["status"] == "available"
    if group_verified:
        found = capture("accounts", "accounts", {}, lambda raw: parse_accounts(raw, scope))
        if found is not None and sources["accounts"]["status"] == "available":
            account_names = found
    else:
        skip("accounts", "accounts", "resource_group_not_verified", {})

    inventory_count = 0
    for region in sorted(catalog.regions):
        account = account_names.get(region)
        target = {"region": region}
        inventory_id = f"deployments:{region}"
        if account:
            target["account"] = account

            def inventory_parser(raw: dict, account_name: str = account, used: int = inventory_count) -> list[dict]:
                parsed = parse_deployments(raw, scope, account_name)
                if used + len(parsed) > MAX_DEPLOYMENTS:
                    raise EvidenceError("deployment_limit_exceeded")
                return parsed

            inventory = capture("deployments", inventory_id, target, inventory_parser, account=account, region=region)
            if inventory is not None:
                inventories[region] = inventory
                inventory_count += len(inventory)
        else:
            skip("deployments", inventory_id, "catalog_account_not_verified", target)
        if group_verified:
            quota = capture("quota", f"quota:{region}", {"region": region}, parse_quota, region=region)
            if quota is not None:
                quotas[region] = quota
        else:
            skip("quota", f"quota:{region}", "resource_group_not_verified", {"region": region})

    model_queries = {}
    for deployment in catalog.deployments:
        model = deployment.model
        model_queries[(model.format, model.name, model.version)] = model
    for key, model in sorted(model_queries.items()):
        source_id = availability_source_id(key)
        target = {"format": model.format, "name": model.name, "version": model.version}
        if group_verified:
            observed = capture(
                "availability", source_id, target,
                lambda raw, m=model: parse_availability(raw, m, catalog), model=model,
            )
            if observed is not None:
                availability[key] = observed
        else:
            skip("availability", source_id, "resource_group_not_verified", target)

    for region in sorted(catalog.regions):
        account = account_names.get(region)
        definition_id, metric_id = f"definitions:{region}", f"metrics:{region}"
        target = {"region": region}
        expected = {d.name for d in catalog.deployments if d.region == region}
        live = {row["name"]: row for row in inventories.get(region, []) if row["name"] in expected}
        if account is None or sources[f"deployments:{region}"]["status"] != "available":
            skip("definitions", definition_id, "deployment_inventory_not_verified", target)
            skip("metrics", metric_id, "deployment_inventory_not_verified", target)
            continue
        target["account"] = account
        definitions = capture("definitions", definition_id, target, parse_definitions, account=account, region=region)
        supported = tuple(name for name in METRICS if definitions is not None and definitions[name] is None)
        definition_transport_codes = sources[definition_id]["codes"][:]
        if definitions is not None and any(definitions.values()):
            sources[definition_id]["status"] = "partial"
            sources[definition_id]["codes"] = sorted({
                *definition_transport_codes, *(code for code in definitions.values() if code),
            })
        if not live or not supported:
            skip("metrics", metric_id, "no_observable_deployments_or_metrics", target)
            continue
        observed = capture(
            "metrics", metric_id, target,
            partial(
                parse_metrics, scope=scope, region=region, account=account, live=live,
                window=window, requested=supported, budget=point_budget,
            ),
            account=account, region=region, window=window, metrics=supported,
        )
        if observed is None:
            continue
        measured, metric_codes, omitted = observed
        excluded_series += omitted
        transport_codes = sources[metric_id]["codes"][:] + definition_transport_codes
        if metric_codes:
            sources[metric_id]["status"] = "partial"
            sources[metric_id]["codes"] = sorted({*sources[metric_id]["codes"], *metric_codes})
        for name in live:
            usage[(region, name)] = {}
            for metric in METRICS:
                value = measured[name].get(metric, unknown_usage(definitions[metric] or "metric_missing"))
                if transport_codes:
                    value["codes"] = sorted({*value["codes"], *transport_codes})
                    value["status"] = "partial" if value["samples"] else "unknown"
                    value["total"] = None
                value["source"] = metric_id
                usage[(region, name)][metric] = value

    declarations = []
    unexpected = []
    for deployment in catalog.deployments:
        source_id = f"deployments:{deployment.region}"
        source = sources[source_id]
        matches = [row for row in inventories.get(deployment.region, []) if row["name"] == deployment.name]
        row = matches[0] if len(matches) == 1 else None
        state = (
            "unknown" if source["status"] != "available"
            else "absent" if row is None
            else "identity_mismatch" if row["model"] != deployment.model
            else "not_succeeded" if row["state"] != "Succeeded"
            else "matched"
        )
        declaration = {
            "catalog": deployment.public(), "account": account_names.get(deployment.region),
            "inventoryStatus": state, "allocationSource": source_id,
            "live": {
                "model": row["model"].public(), "capacity": row["capacity"], "unit": "raw_capacity_units",
                "provisioningState": row["state"], "retrievedAt": source["finishedAt"],
            } if row is not None else None,
            "usage": usage.get((deployment.region, deployment.name), {
                metric: {**unknown_usage("deployment_or_metric_source_unavailable"), "source": f"metrics:{deployment.region}"}
                for metric in METRICS
            }),
        }
        declarations.append(declaration)
    for region, inventory in sorted(inventories.items()):
        expected = {d.name for d in catalog.deployments if d.region == region}
        for row in inventory:
            if row["name"] not in expected:
                unexpected.append({
                    "region": region, "account": account_names[region], "name": row["name"],
                    "model": row["model"].public(), "capacity": row["capacity"], "unit": "raw_capacity_units",
                    "allocationSource": f"deployments:{region}", "usage": "not_collected",
                })

    evidence = None
    evidence_error = None
    evidence_digest = None
    if pool_path is not None:
        try:
            document, evidence_digest = read_json(pool_path, MAX_EVIDENCE_BYTES)
            evidence = parse_pool_evidence(document, scope, now())
        except EvidenceError as exc:
            evidence_error = exc.code
    pools, pool_coverage = pool_rollups(
        evidence, catalog, inventories, quotas, availability, sources, evidence_error,
    )
    measurement_codes = set()
    if any(source["status"] != "available" for source in sources.values()):
        measurement_codes.add("incomplete_sources")
    if any(row["inventoryStatus"] != "matched" for row in declarations):
        measurement_codes.add("incomplete_catalog_inventory")
    if any(value["status"] != "measured" for row in declarations for value in row["usage"].values()):
        measurement_codes.add("incomplete_usage")
    complete = not measurement_codes and pool_coverage["status"] == "consistent"
    return {
        "schemaVersion": 1, "status": "complete" if complete else "partial",
        "startedAt": utc_text(started), "finishedAt": utc_text(now()),
        "scope": scope.public(), "window": window.public(),
        "catalog": {"sha256": catalog.digest, "path": "infra/models.json", "declaredAt": None},
        "measurementCoverage": {"status": "complete" if not measurement_codes else "partial", "codes": sorted(measurement_codes)},
        "sources": list(sources.values()), "deployments": declarations,
        "uncataloguedDeployments": unexpected, "quotaCounters": counter_groups(quotas, sources),
        "platformAvailability": [
            {**row, "source": availability_source_id(key), "retrievedAt": sources[availability_source_id(key)]["finishedAt"]}
            for key, items in sorted(availability.items()) for row in items
        ],
        "poolEvidence": {
            "authority": "operator_asserted" if evidence is not None else "not_established",
            "sha256": evidence_digest, "error": evidence_error,
            "observedAt": evidence["observedAt"] if evidence else None,
            "reference": evidence["reference"] if evidence else None,
        },
        "pools": pools, "poolCoverage": pool_coverage,
        "exclusions": [
            "Legacy AzureOpenAIRequests/TokenTransaction/ProcessedPromptTokens/GeneratedTokens are not added to canonical metrics.",
            "Voice Live and service-specific metrics without the deployment/model/version dimensions are not attributed.",
            "Metrics for uncatalogued or deleted deployments are excluded; observed series do not prove complete workload coverage.",
            "No prompts, replies, user identities, tool calls, logs or traces are queried.",
        ],
        "excludedMetricSeries": excluded_series,
        "cost": {"status": "unknown", "reason": "no_billing_or_pricing_evidence"},
        "policy": "not_evaluated", "recommendations": [], "writes": "none",
        "limits": {
            "regions": MAX_REGIONS, "accountsInInventory": MAX_ACCOUNTS, "deployments": MAX_DEPLOYMENTS,
            "accountPages": MAX_ACCOUNT_PAGES, "accountContinuationBytes": MAX_ACCOUNT_LINK_BYTES,
            "accountCursorBytes": MAX_ACCOUNT_CURSOR_BYTES,
            "modelVersions": MAX_MODELS, "rowsPerMetadataSource": MAX_ROWS,
            "seriesPerMetric": MAX_SERIES, "pointsPerSeries": MAX_POINTS,
            "totalPoints": MAX_TOTAL_POINTS, "responseBytes": MAX_RESPONSE_BYTES,
            "totalResponseBytes": MAX_TOTAL_RESPONSE_BYTES, "reportBytes": MAX_REPORT_BYTES,
            "sourceSeconds": SOURCE_SECONDS, "collectionSeconds": COLLECTION_SECONDS, "calls": MAX_CALLS,
        },
        "consumed": {"calls": reader.calls, "responseBudgetBytes": reader.bytes, "points": MAX_TOTAL_POINTS - point_budget.remaining},
    }


def render(report: dict, output_format: str) -> str:
    serialized = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(serialized.encode("utf-8")) > MAX_REPORT_BYTES:
        raise EvidenceError("report_too_large")
    if output_format == "json":
        return serialized
    lines = [
        f"Capacity evidence: {report['status']} (read-only; no sizing recommendations)",
        f"UTC window: {report['window']['start']} to {report['window']['endExclusive']} (exclusive), hourly Total",
        f"Collected: {report['startedAt']} to {report['finishedAt']}",
        f"Scope: {report['scope']['resourceGroup']} / {report['scope']['environment']}",
        f"Subscription SHA-256: {report['scope']['subscriptionFingerprint']}",
        f"Catalog SHA-256: {report['catalog']['sha256']}",
        f"Pool authority: {report['poolEvidence']['authority']}; coverage: {report['poolCoverage']['status']}",
        "Capacity is in raw deployment units; request/token counters are Count, not TPM or cost.",
    ]
    for row in report["deployments"]:
        catalog = row["catalog"]
        live = row["live"]
        identity = live["model"] if live else catalog["model"]
        capacity = str(live["capacity"]) if live else "unknown"
        counts = []
        for metric, value in row["usage"].items():
            total = str(value["total"]) if value["total"] is not None else "unknown"
            counts.append(f"{metric}={total} [{value['status']}; samples={value['samples']}]")
        lines.extend([
            f"{catalog['name']} / {catalog['region']}: {row['inventoryStatus']}",
            f"  {identity['format']} {identity['name']} version={identity['version']} SKU={identity['sku']} capacity={capacity}",
            "  " + "; ".join(counts),
        ])
    for pool in report["pools"]:
        lines.append(
            f"Pool {pool['counter']} / {pool['scope']} [{pool['unit']}; operator-asserted]: "
            f"{pool['status']}; headroom={pool['headroom'] if pool['headroom'] is not None else 'unknown'}"
        )
    lines.append("Source coverage (raw counter and platform observations are retained in --format json):")
    for source in report["sources"]:
        lines.append(f"  {source['id']}: {source['status']} ({', '.join(source['codes']) or 'observed'}) at {source['finishedAt']}")
        for page in source.get("pages", []):
            names = ", ".join(row["name"] for row in page["accounts"]) or "none"
            lines.append(f"    page {page['page']}: {page['status']}; observed account candidates: {names}")
    lines.append("Pool coverage: " + (", ".join(report["poolCoverage"]["codes"]) or "consistent under operator assertions"))
    lines.extend(report["exclusions"])
    lines.append("Cost, production criticality, reserves and deployable headroom remain unknown/not evaluated.")
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise EvidenceError("report_too_large")
    return text
