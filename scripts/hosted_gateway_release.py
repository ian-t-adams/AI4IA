"""Hosted OIDC release lane for the shared model gateway cutover.

One dispatch applies one stage to production through scoped ARM requests:

  c1-web / c1-api   Single -> Multiple mode, traffic pinned to the serving R0.
  c2-web / c2-api   new digest-pinned image revision at 0%, healthy, promote, deactivate R0.
  c3-api            R2 = R1-OFF + shared gateway selector/opt-in/manifest at 0%,
                    in-revision smoke, promote; R1 stays active as the rollback.
  rollback-api      move traffic back to a named, active, gateway-off bc58 revision.

Never sends configuration.secrets, never deletes, never replays an uncertain
write, and never reactivates a pre-release revision automatically.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import http.client
import http.client as http_client
import importlib.util
import io
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urlsplit

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]
TENANT = "6907d2a4-685a-4aea-92ab-d930217467f1"
SUBSCRIPTION = "e852113b-6cb5-441c-ac68-26cff884e479"
CLIENT = "ac8d62b9-904f-4c84-9444-c41512acf74f"
REPOSITORY = "ian-t-adams/AI4IA"
RG = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-ai4ia-slurmfactory"
APPS = {
    "api": RG + "/providers/Microsoft.App/containerApps/ca-api-private-slurmfactory",
    "web": RG + "/providers/Microsoft.App/containerApps/ca-web-slurmfactory",
}
IMAGES = {
    "api": "acrai4iavypvgrncoed2o.azurecr.io/ai4ia/api-slurmfactory"
           "@sha256:d77ede7784873919c413b78129656170885acd181f3e223e145fd12368b13c3e",
    "web": "acrai4iavypvgrncoed2o.azurecr.io/ai4ia/web-slurmfactory"
           "@sha256:538422620463e7d64335710883a57077c8fbbb25b8de9eef58c5a4899bb26768",
}
DEPLOYMENTS = (
    "gpt-5.4-mini-slurmfactory-eastus2-dz",
    "gpt-5.4-mini-slurmfactory-eastus2-glbl",
    "gpt-5.4-mini-slurmfactory-swedencentral-dz",
    "gpt-5.4-mini-slurmfactory-swedencentral-glbl",
)
DEPLOYMENTS_BYTES = ",".join(DEPLOYMENTS).encode("ascii")
DEPLOYMENTS_SHA256 = hashlib.sha256(DEPLOYMENTS_BYTES).hexdigest()
MANIFEST_SHA256 = "94c2edf581b40a2677f3f90fad76eb2fc957478e5969c5007132d35040e72cb5"
MANIFEST_SIZE = 24569
SIDECAR_SHA256 = "f73a55378dbe418e92ca4ea3d0bc8e30ef6ae09778f3aebddc3525e5b571ecc4"
SIDECAR_SIZE = 120445
SIDECAR_B64_MAX = 8192
DTS_ENDPOINT = "https://dts-ai4ia-slurm-dratcma7.eastus2.durabletask.io"
DTS_TASK_HUB = "ai4ia"
DTS_METRICS = ("OrchestratorActiveItems", "OrchestratorPendingItems",
               "ActivityActiveItems", "ActivityPendingItems")
GATE_MAX_AGE = 300
GATE_REFRESH_AGE = 180
APP_API = "2025-01-01"
DTS_API = "2026-02-01"
METRICS_API = "2023-10-01"
ARM_HOST = "management.azure.com"
TOKEN_HOST = "login.microsoftonline.com"

AUDIENCE = "api://AzureADTokenExchange"
SUBJECT = f"repo:{REPOSITORY}:environment:production"
MAX_BODY = 4 * 1024 * 1024
STAGES = ("c1-web", "c1-api", "c2-web", "c2-api", "c3-api", "rollback-api")
SMOKE_MARK = "AI4IA_SMOKE" + "_RESULT="


class Refused(Exception):
    """A deliberate stop. ``phase`` is an allowlisted label, never response text."""

    def __init__(self, phase: str, status: int | None = None):
        super().__init__(phase)
        self.phase = phase
        self.status = status if type(status) is int and 100 <= status <= 599 else None


class Uncertain(Refused):
    """A write whose outcome is unknown. Reconcile by reading; never replay."""


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _oidc():
    return _load("_ai4ia_hosted_oidc_read", ROOT / "scripts" / "hosted_oidc_read.py")


def cutover():
    return _load("_ai4ia_prepare_gateway_cutover", ROOT / "scripts" / "prepare-gateway-cutover.py")


def build_payload(*args: Any, **kwargs: Any) -> dict:
    """The reviewed cutover builder; its validation messages never reach receipts."""
    try:
        return cutover().build_payload(*args, **kwargs)
    except (ValueError, KeyError, TypeError):
        raise Refused("payload") from None


# --------------------------------------------------------------------------- admission


def validate_environment(env: dict[str, str]) -> str:
    expected = {
        "AI4IA_GATEWAY_RELEASE_ENABLED": "true",
        "AI4IA_GATEWAY_RELEASE_ADMIT": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": REPOSITORY,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_CLIENT_ID": CLIENT,
    }
    if any(env.get(k) != v for k, v in expected.items()):
        raise Refused("admission")
    sha = env.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or env.get("AI4IA_REVIEWED_SHA") != sha:
        raise Refused("admission")
    if not re.fullmatch(r"[0-9]{1,20}", env.get("GITHUB_RUN_ID", "")):
        raise Refused("admission")
    stage = env.get("AI4IA_RELEASE_STAGE", "")
    if stage not in STAGES:
        raise Refused("admission")
    rollback = env.get("AI4IA_ROLLBACK_REVISION", "")
    if (stage == "rollback-api") != bool(rollback):
        raise Refused("admission")
    denied = {
        "http_proxy", "https_proxy", "all_proxy", "no_proxy", "requests_ca_bundle",
        "curl_ca_bundle", "ssl_cert_file", "ssl_cert_dir", "sslkeylogfile", "pythonhttpsverify",
    }
    if any(k.lower() in denied and value for k, value in env.items()):
        raise Refused("environment")
    return stage


def candidate_suffix(stage: str, sha: str, run_id: str) -> str:
    return f"{stage.split('-')[0]}-{sha[:7]}-{run_id[-8:]}"


# --------------------------------------------------------------------------- inputs


def load_manifest(env: dict[str, str]) -> bytes:
    raw = env.get("AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST", "")
    data = raw.encode("utf-8")
    if len(data) != MANIFEST_SIZE or hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
        raise Refused("manifest")
    return data


def load_sidecar(env: dict[str, str]) -> bytes:
    raw = env.get("AI4IA_SHARED_GATEWAY_BINDING_SIDECAR_GZIP_B64", "")
    if not raw or len(raw) > SIDECAR_B64_MAX or not raw.isascii():
        raise Refused("sidecar")
    try:
        packed = base64.b64decode(raw.encode("ascii"), validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(packed), mode="rb") as stream:
            data = stream.read(SIDECAR_SIZE + 1)
            trailing = stream.read(1)
    except (binascii.Error, OSError, EOFError, ValueError):
        raise Refused("sidecar") from None
    if (trailing or len(data) != SIDECAR_SIZE
            or hashlib.sha256(data).hexdigest() != SIDECAR_SHA256):
        raise Refused("sidecar")
    return data


# --------------------------------------------------------------------------- HTTP


def _url_parts(url: str, hosts: set[str]) -> tuple[str, str]:
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname not in hosts or p.port not in (None, 443)
            or p.username or p.password or p.fragment or "\\" in url):
        raise Refused("url")
    return p.hostname, (p.path or "/") + ("?" + p.query if p.query else "")


def http_request(method: str, url: str, *, hosts: set[str], headers: dict[str, str] | None = None,
                 body: bytes | None = None, limit: int = MAX_BODY, timeout: float = 30,
                 phase: str = "arm") -> tuple[int, dict[str, str], bytes]:
    """Direct TLS request: no proxy, no redirect, identity encoding, bounded body."""
    host, target = _url_parts(url, hosts)
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout,
                                       context=ssl.create_default_context())
    try:
        conn.request(method, target, body=body,
                     headers={**(headers or {}), "Accept-Encoding": "identity"})
        response = conn.getresponse()
        status = response.status
        lowered = {k.lower(): v for k, v in response.getheaders()}
        if lowered.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            raise Refused(phase, status)
        data = response.read(limit + 1)
        if len(data) > limit:
            raise Refused(phase, status)
        return status, lowered, data
    finally:
        conn.close()


def acquire_arm_token(env: dict[str, str], now: float, http=http_request) -> str:
    oidc = _oidc()
    try:
        url = oidc.oidc_url(env.get("ACTIONS_ID_TOKEN_REQUEST_URL"))
    except oidc.Refused as e:
        raise Refused(e.phase) from None
    runtime = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not runtime or len(runtime) > 16384:
        raise Refused("assertion-runtime-token")
    try:
        status, _, body = http("GET", url, hosts={urlsplit(url).hostname}, limit=65536,
                               phase="assertion-response",
                               headers={"Authorization": "Bearer " + runtime})
    except (OSError, http_client.HTTPException):
        raise Refused("assertion-transport") from None
    if status != 200:
        raise Refused("assertion-response", status)
    try:
        value = json.loads(body)["value"]
    except (ValueError, TypeError, KeyError):
        raise Refused("assertion-response", status) from None
    try:
        assertion = oidc.check_assertion(value, env["GITHUB_SHA"], int(now))
    except oidc.Refused as e:
        raise Refused(e.phase, status) from None
    form = urlencode({
        "client_assertion": assertion,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_id": CLIENT,
        "grant_type": "client_credentials",
        "scope": "https://management.azure.com/.default",
    }).encode("ascii")
    try:
        status, _, body = http("POST", f"https://{TOKEN_HOST}/{TENANT}/oauth2/v2.0/token",
                               hosts={TOKEN_HOST}, body=form, limit=32768, phase="credential",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    except (OSError, http_client.HTTPException):
        raise Refused("credential") from None
    if status != 200:
        raise Refused("credential", status)
    try:
        token = json.loads(body)["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise Refused("credential", status) from None
    return token


class Arm:
    def __init__(self, token: str, http=http_request, sleep=time.sleep, clock=time.monotonic):
        self._token = token
        self._http = http
        self.sleep = sleep
        self.clock = clock
        self.writes = 0

    def _call(self, method, url, body=None, phase="arm"):
        headers = {"Authorization": "Bearer " + self._token}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        return self._http(method, url, hosts={ARM_HOST}, headers=headers, body=payload,
                          phase=phase)

    @staticmethod
    def url(path: str, api: str = APP_API, **query: str) -> str:
        return f"https://{ARM_HOST}{path}?" + urlencode({"api-version": api, **query})

    def get(self, path: str, api: str = APP_API, phase: str = "arm", **query: str) -> dict:
        status, _, body = self._call("GET", self.url(path, api, **query), phase=phase)
        if status != 200:
            raise Refused(phase, status)
        try:
            value = json.loads(body)
        except ValueError:
            raise Refused(phase, status) from None
        if not isinstance(value, dict):
            raise Refused(phase, status)
        return value

    def list(self, path: str, api: str = APP_API, phase: str = "arm", pages: int = 20) -> list:
        url, rows = self.url(path, api), []
        for _ in range(pages):
            status, _, body = self._call("GET", url, phase=phase)
            if status != 200:
                raise Refused(phase, status)
            try:
                page = json.loads(body)
                rows.extend(page["value"])
            except (ValueError, KeyError, TypeError):
                raise Refused(phase, status) from None
            url = page.get("nextLink")
            if not url:
                return rows
            _url_parts(url, {ARM_HOST})
        raise Refused(phase)

    def write(self, method: str, path: str, body: dict | None = None, *,
              timeout: float = 600, phase: str = "write") -> None:
        """Send exactly one write. Any transport doubt becomes Uncertain, never a retry."""
        self.writes += 1
        try:
            status, headers, _ = self._call(method, self.url(path), body, phase=phase)
        except Refused as e:
            raise Uncertain(phase, e.status) from None
        except (OSError, http.client.HTTPException):
            raise Uncertain(phase) from None
        if 400 <= status <= 499:
            raise Refused(phase, status)
        if status not in (200, 201, 202, 204):
            raise Uncertain(phase, status)
        poll = headers.get("azure-asyncoperation") or (headers.get("location") if status == 202 else None)
        if poll:
            self._poll(poll, timeout, phase)

    def _poll(self, url: str, timeout: float, phase: str) -> None:
        try:
            _url_parts(url, {ARM_HOST})
        except Refused:
            raise Uncertain(phase) from None
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            self.sleep(5)
            try:
                status, _, body = self._call("GET", url, phase=phase)
            except (Refused, OSError, http.client.HTTPException):
                continue
            if status == 202:
                continue
            if status != 200:
                raise Uncertain(phase, status)
            try:
                state = json.loads(body).get("status")
            except (ValueError, AttributeError):
                raise Uncertain(phase, status) from None
            if state == "Succeeded":
                return
            if state in ("Failed", "Canceled"):
                raise Uncertain(phase + "-failed", status)
            if state not in ("Accepted", "Running", "InProgress"):
                raise Uncertain(phase, status)
        raise Uncertain(phase + "-timeout")


# --------------------------------------------------------------------------- DTS gate


class DtsGate:
    """Metadata-only DTS metrics read. Missing, null, stale or nonzero refuses."""

    def __init__(self, arm: Arm, now=lambda: datetime.now(UTC), attempts: int = 6,
                 wait: float = 30):
        self.arm, self.now, self.attempts, self.wait = arm, now, attempts, wait
        self.scheduler: str | None = None
        self.observed: datetime | None = None
        self.reads = 0

    def _scheduler(self) -> str:
        if self.scheduler is None:
            rows = self.arm.list(RG + "/providers/Microsoft.DurableTask/schedulers", DTS_API,
                                 phase="dts-scheduler")
            matches = [r for r in rows if isinstance(r, dict)
                       and (r.get("properties") or {}).get("endpoint", "").rstrip("/") == DTS_ENDPOINT]
            if len(matches) != 1 or not str(matches[0].get("id", "")).lower().startswith(
                    (RG + "/providers/Microsoft.DurableTask/schedulers/").lower()):
                raise Refused("dts-scheduler")
            self.scheduler = matches[0]["id"]
        return self.scheduler

    def observe(self) -> datetime | None:
        now = self.now()
        self.reads += 1
        value = self.arm.get(
            self._scheduler() + "/providers/Microsoft.Insights/metrics", METRICS_API,
            phase="dts-metrics",
            metricnames=",".join(DTS_METRICS), aggregation="Maximum", interval="PT1M",
            timespan=f"{(now - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')}/"
                     f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            **{"$filter": f"TaskHubName eq '{DTS_TASK_HUB}'"},
        )
        return latest_zero_observation(value, now)

    def require(self) -> None:
        for attempt in range(self.attempts):
            observed = self.observe()
            if observed is not None:
                self.observed = observed
                return
            if attempt + 1 < self.attempts:
                self.arm.sleep(self.wait)
        raise Refused("dts-gate")

    def require_fresh(self) -> None:
        """Re-read unless the latest actual all-zero observation is still recent."""
        if self.observed is None or (self.now() - self.observed).total_seconds() > GATE_REFRESH_AGE:
            self.require()


def latest_zero_observation(value: Any, now: datetime) -> datetime | None:
    """Require all four metrics to be zero at the newest reported timestamp."""
    try:
        metrics = value["value"]
        series: dict[str, dict[datetime, Any]] = {}
        for metric in metrics:
            name = metric["name"]["value"]
            if name not in DTS_METRICS or name in series:
                return None
            ts = metric["timeseries"]
            if len(ts) != 1:
                return None
            points: dict[datetime, Any] = {}
            for point in ts[0]["data"]:
                stamp = datetime.fromisoformat(point["timeStamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp in points:
                    return None
                points[stamp] = point.get("maximum")
            series[name] = points
        if set(series) != set(DTS_METRICS):
            return None
        stamps = {stamp for points in series.values() for stamp in points}
        if not stamps:
            return None
        observed = max(stamps)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None
    age = (now - observed).total_seconds()
    if age < -60 or age > GATE_MAX_AGE:
        return None
    if any(type(series[m].get(observed)) not in (int, float) or series[m][observed] != 0
           for m in DTS_METRICS):
        return None
    return observed


# --------------------------------------------------------------------------- state


def projection(app: dict) -> dict:
    props = app.get("properties") or {}
    config = props.get("configuration") or {}
    return {
        "provisioningState": props.get("provisioningState"),
        "latestRevisionName": props.get("latestRevisionName"),
        "latestReadyRevisionName": props.get("latestReadyRevisionName"),
        "activeRevisionsMode": config.get("activeRevisionsMode"),
        "traffic": (config.get("ingress") or {}).get("traffic"),
        "template": cutover().writable_template_projection(props.get("template") or {}),
    }


def revision_rows(arm: Arm, app_id: str, now: datetime) -> dict:
    rows = []
    for item in arm.list(app_id + "/revisions", phase="revisions"):
        props = item.get("properties") or {}
        rows.append({"name": item.get("name"), "active": props.get("active"),
                     "health": props.get("healthState"), "state": props.get("runningState")})
    return {"observedAt": now.isoformat(), "rows": rows}


def traffic_is(app: dict, revision: str) -> bool:
    traffic = (((app.get("properties") or {}).get("configuration") or {}).get("ingress") or {}).get("traffic")
    return traffic == [{"revisionName": revision, "weight": 100}]


def ready(revision: dict) -> bool:
    props = revision.get("properties") or {}
    return (props.get("provisioningState") == "Provisioned" and props.get("active") is True
            and props.get("healthState") == "Healthy"
            and str(props.get("runningState", "")).startswith("Running")
            and type(props.get("replicas")) is int and props["replicas"] >= 1)


def env_value(revision: dict, name: str) -> Any:
    containers = ((revision.get("properties") or {}).get("template") or {}).get("containers") or []
    if len(containers) != 1:
        return ...
    for item in containers[0].get("env") or []:
        if item.get("name", "").upper() == name:
            return item if set(item) != {"name", "value"} else item["value"]
    return None


def gateway_off_release(revision: dict) -> bool:
    containers = ((revision.get("properties") or {}).get("template") or {}).get("containers") or []
    return (ready(revision) and len(containers) == 1 and containers[0].get("image") == IMAGES["api"]
            and env_value(revision, "AI4IA_SHARED_GATEWAY_ENABLED") in (None, "false")
            and env_value(revision, "AI4IA_SHARED_GATEWAY_DEPLOYMENTS") is None
            and env_value(revision, "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST") is None)


def template_matches(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    try:
        project = cutover().writable_template_projection
        return project(actual) == project(expected)
    except (ValueError, KeyError, TypeError):
        return False


# --------------------------------------------------------------------------- exec smoke


SMOKE_PROGRAM = r'''
import asyncio, hashlib, json, logging, sys
logging.disable(logging.CRITICAL)
EXPECTED = frozenset(__DEPLOYMENTS__)
MARK = "AI4IA_SMOKE" + "_RESULT="

async def run():
    out = {"phase": "settings", "calls": [], "base": None}
    try:
        from urllib.parse import urlsplit
        import httpx
        from ai4ia_api.catalog import load_catalog
        from ai4ia_api.config import get_settings
        from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
        s = get_settings()
        if (not s.shared_gateway_enabled or s.chat_pilot_isolation_enabled
                or s.shared_gateway_deployment_set != EXPECTED):
            out["phase"] = "config"
            return out
        out["phase"] = "catalog"
        catalog = load_catalog(s.model_catalog_path, s.data_residency, s.claude_runtime_enabled)
        skus = {}
        for model in catalog.models:
            for option in model.options:
                if option.deploymentName in EXPECTED:
                    skus.setdefault(option.deploymentName, set()).add(option.sku)
        if set(skus) != EXPECTED or any(len(v) != 1 for v in skus.values()):
            return out
        # Operator canary: there is no end user, so per-user quota/policy
        # accounting is not engaged. Route, identity and manifest are unchanged.
        s = s.model_copy(update={"hard_quota_enabled": False, "group_policy_enabled": False})
        out["phase"] = "client"
        seen = []

        async def hook(response):
            seen.append(response.request.url.host)

        async with httpx.AsyncClient(timeout=s.gateway_timeout_seconds, trust_env=True,
                                     event_hooks={"response": [hook]}) as http:
            gateway = ModelGatewayClient(s, http_client=http)
            base = urlsplit(gateway._shared_manifest.base_url).hostname
            out["base"] = hashlib.sha256(base.encode()).hexdigest()
            original = gateway._shared_request
            shared = []

            async def observed(*args, **kwargs):
                request, url = await original(*args, **kwargs)
                shared.append(url is not None)
                return request, url

            gateway._shared_request = observed
            out["phase"] = "calls"
            try:
                for deployment in sorted(EXPECTED):
                    for api in ("chat", "responses"):
                        seen.clear()
                        shared.clear()
                        status = None
                        try:
                            await gateway.complete(
                                deployment=deployment,
                                messages=[{"role": "user", "content": "Reply with OK."}],
                                api=api, shared_gateway_eligible=True,
                                shared_gateway_sku=next(iter(skus[deployment])),
                            )
                            status = 200
                        except ModelGatewayError as exc:
                            code = getattr(exc, "status_code", None)
                            status = code if type(code) is int and 100 <= code <= 599 else None
                        except Exception:
                            status = None
                        out["calls"].append({
                            "deployment": deployment, "api": api, "status": status,
                            "shared": shared == [True],
                            "host": bool(seen) and all(h == base for h in seen),
                        })
            finally:
                await gateway.close()
        out["phase"] = "complete"
    except Exception:
        pass
    return out

result = asyncio.run(run())
sys.stdout.write("\n" + MARK + json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
sys.stdout.flush()
'''.replace("__DEPLOYMENTS__", repr(list(DEPLOYMENTS)))


def smoke_passed(result: Any) -> bool:
    if not isinstance(result, dict) or result.get("phase") != "complete":
        return False
    calls = result.get("calls")
    expected = {(d, api) for d in DEPLOYMENTS for api in ("chat", "responses")}
    if not isinstance(calls, list) or len(calls) != len(expected):
        return False
    got = set()
    for call in calls:
        if (not isinstance(call, dict) or call.get("status") != 200
                or call.get("shared") is not True or call.get("host") is not True):
            return False
        got.add((call.get("deployment"), call.get("api")))
    return got == expected and isinstance(result.get("base"), str)


def sanitize_smoke(result: Any) -> dict:
    """Keep only allowlisted, content-free fields for receipts."""
    if not isinstance(result, dict):
        return {"phase": None, "calls": []}
    calls = []
    for call in result.get("calls") or []:
        if not isinstance(call, dict) or call.get("deployment") not in DEPLOYMENTS:
            continue
        status = call.get("status")
        calls.append({
            "deployment": call["deployment"],
            "api": call.get("api") if call.get("api") in ("chat", "responses") else None,
            "status": status if type(status) is int and 100 <= status <= 599 else None,
            "shared": call.get("shared") is True,
            "host": call.get("host") is True,
        })
    phase = result.get("phase")
    base = result.get("base")
    return {
        "phase": phase if phase in ("settings", "config", "catalog", "client", "calls", "complete") else None,
        "base": base if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{64}", base) else None,
        "calls": calls[:8],
    }


def shell_lines(program: str, width: int = 900) -> list[bytes]:
    """Feed a base64 program through a TTY's line discipline in short lines."""
    encoded = base64.b64encode(program.encode("utf-8")).decode("ascii")
    chunks = [encoded[i:i + width] for i in range(0, len(encoded), width)]
    lines = ["stty -echo 2>/dev/null; S=''"]
    lines += [f"S=\"$S{chunk}\"" for chunk in chunks]
    lines.append('/opt/venv/bin/python -I -c "$(printf %s "$S" | base64 -d)"; exit')
    return [(line + "\n").encode("ascii") for line in lines]


def ws_frame(payload: bytes, opcode: int = 0x2, mask: bytes | None = None) -> bytes:
    mask = mask if mask is not None else os.urandom(4)
    header = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header += bytes([0x80 | size])
    elif size < 65536:
        header += bytes([0x80 | 126]) + size.to_bytes(2, "big")
    else:
        header += bytes([0x80 | 127]) + size.to_bytes(8, "big")
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _read_exact(sock, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise Refused("smoke-transport")
        data.extend(chunk)
    return bytes(data)


def ws_read(sock, limit: int = 65536) -> tuple[int, bytes]:
    first, second = _read_exact(sock, 2)
    opcode, size = first & 0x0F, second & 0x7F
    if second & 0x80:
        raise Refused("smoke-transport")
    if size == 126:
        size = int.from_bytes(_read_exact(sock, 2), "big")
    elif size == 127:
        size = int.from_bytes(_read_exact(sock, 8), "big")
    if size > limit:
        raise Refused("smoke-transport")
    return opcode, _read_exact(sock, size)


def ws_handshake(sock, host: str, path: str, token: str, key: bytes | None = None) -> None:
    key_text = base64.b64encode(key or os.urandom(16)).decode("ascii")
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key_text}\r\nSec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n\r\n"
    ).encode("ascii"))
    head = bytearray()
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        if not chunk or len(head) > 16384:
            raise Refused("smoke-handshake")
        head.extend(chunk)
    lines = bytes(head).decode("latin-1").split("\r\n")
    match = re.fullmatch(r"HTTP/1\.1 (\d{3})(?: .*)?", lines[0])
    status = int(match.group(1)) if match else None
    if status != 101:
        raise Refused("smoke-handshake", status)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    accept = base64.b64encode(hashlib.sha1(
        (key_text + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    if headers.get("sec-websocket-accept") != accept:
        raise Refused("smoke-handshake", status)


def exec_smoke(sock, lines: list[bytes], *, deadline: float, clock=time.monotonic,
               cap: int = 65536) -> Any:
    """Send stdin lines, then parse only the program's marker line from stdout."""
    for line in lines:
        sock.sendall(ws_frame(b"\x00\x00" + line))
    output = bytearray()
    while clock() < deadline:
        opcode, payload = ws_read(sock)
        if opcode == 0x8:
            raise Refused("smoke-transport")
        if opcode == 0x9:
            sock.sendall(ws_frame(payload, 0xA))
            continue
        if opcode in (0x1, 0x2) and payload[:1] == b"\x02":
            raise Refused("smoke-transport")
        if opcode not in (0x0, 0x1, 0x2) or len(payload) < 2 or payload[0] != 0:
            continue
        if payload[1] in (1, 2):
            output.extend(payload[2:])
            if len(output) > cap:
                raise Refused("smoke-output")
            text = output.decode("utf-8", "replace")
            index = text.find(SMOKE_MARK)
            if index >= 0:
                rest = text[index + len(SMOKE_MARK):]
                end = min((i for i in (rest.find("\r"), rest.find("\n")) if i >= 0), default=-1)
                if end >= 0:
                    try:
                        return json.loads(rest[:end])
                    except ValueError:
                        raise Refused("smoke-output") from None
    raise Refused("smoke-timeout")


def run_smoke(arm: Arm, app_id: str, revision: str, connect=None, clock=time.monotonic) -> dict:
    replicas = arm.list(f"{app_id}/revisions/{revision}/replicas", phase="smoke-replicas")
    names = [r.get("name") for r in replicas if isinstance(r, dict)
             and str((r.get("properties") or {}).get("runningState", "")).startswith("Running")]
    if not names or not all(isinstance(n, str) and re.fullmatch(r"[a-z0-9-]{1,128}", n) for n in names):
        raise Refused("smoke-replicas")
    replica = min(names)
    detail = arm.get(f"{app_id}/revisions/{revision}/replicas/{replica}", phase="smoke-replicas")
    containers = [c for c in (detail.get("properties") or {}).get("containers") or []
                  if c.get("name") == "api"]
    if len(containers) != 1:
        raise Refused("smoke-replicas")
    endpoint = urlsplit(str(containers[0].get("logStreamEndpoint", "")))
    host = endpoint.hostname or ""
    if (endpoint.scheme != "https" or endpoint.port not in (None, 443)
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.azurecontainerapps\.dev", host)):
        raise Refused("smoke-endpoint")
    token_status, _, token_body = arm._call("POST", arm.url(app_id + "/getAuthToken"),
                                            phase="smoke-token")
    if token_status != 200:
        raise Refused("smoke-token", token_status)
    try:
        token = json.loads(token_body)["properties"]["token"]
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise Refused("smoke-token", token_status) from None
    app_name = app_id.rsplit("/", 1)[-1]
    path = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-ai4ia-slurmfactory/containerApps/"
            f"{app_name}/revisions/{quote(revision)}/replicas/{quote(replica)}/containers/api/exec"
            f"?command={quote_plus('/bin/sh')}")
    if connect is None:
        def connect(h):
            raw = socket.create_connection((h, 443), timeout=30)
            return ssl.create_default_context().wrap_socket(raw, server_hostname=h)
    sock = connect(host)
    try:
        sock.settimeout(30)
        ws_handshake(sock, host, path, token)
        result = exec_smoke(sock, shell_lines(SMOKE_PROGRAM), deadline=clock() + 240, clock=clock)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return sanitize_smoke(result)


# --------------------------------------------------------------------------- stages


class Release:
    def __init__(self, arm: Arm, env: dict[str, str], receipt: dict, gate: DtsGate | None = None,
                 now=lambda: datetime.now(UTC), smoke=run_smoke):
        self.arm, self.env, self.receipt, self.now, self.smoke = arm, env, receipt, now, smoke
        self.gate = gate or DtsGate(arm, now=now)

    def step(self, name: str, **fields: Any) -> None:
        self.receipt["steps"].append({"step": name, "at": self.now().isoformat(), **fields})

    def snapshot(self, service: str) -> tuple[dict, dict]:
        app = self.arm.get(APPS[service], phase="read")
        r0 = (app.get("properties") or {}).get("latestRevisionName")
        if not isinstance(r0, str):
            raise Refused("read")
        return app, self.arm.get(f"{APPS[service]}/revisions/{r0}", phase="read")

    def guarded_patch(self, service: str, expected: dict, payload: dict, name: str) -> None:
        """Re-read immediately before one PATCH. Not atomic CAS (no If-Match support)."""
        if projection(self.arm.get(APPS[service], phase="reread")) != expected:
            raise Refused("concurrent-change")
        self.step(name, write="PATCH")
        self.arm.write("PATCH", APPS[service], payload, phase=name)

    def wait_ready(self, service: str, revision: str, timeout: float = 900) -> dict:
        deadline = self.arm.clock() + timeout
        while self.arm.clock() < deadline:
            try:
                rev = self.arm.get(f"{APPS[service]}/revisions/{revision}", phase="wait")
            except Refused as e:
                if e.status != 404:
                    raise
                rev = {}
            state = (rev.get("properties") or {}).get("provisioningState")
            if state == "Failed":
                raise Refused("candidate-failed")
            if ready(rev):
                return rev
            self.arm.sleep(15)
        raise Refused("candidate-timeout")

    def c1(self, service: str) -> None:
        app, rev = self.snapshot(service)
        payload = build_payload(app, rev, resource_id=APPS[service], stage="C1", service=service)
        r0 = rev["name"]
        self.receipt["r0"] = r0
        self.guarded_patch(service, projection(app), payload, "c1-patch")
        after = self.arm.get(APPS[service], phase="readback")
        if (after["properties"]["configuration"].get("activeRevisionsMode") != "Multiple"
                or not traffic_is(after, r0) or after["properties"].get("latestRevisionName") != r0):
            raise Refused("c1-readback")
        self.step("c1-verified", revision=r0)

    def create_and_promote(self, service: str, stage: str, **inputs: Any) -> None:
        sha, run = self.env["GITHUB_SHA"], self.env["GITHUB_RUN_ID"]
        suffix = candidate_suffix(stage.lower(), sha, run)
        app_id = APPS[service]
        app, rev = self.snapshot(service)
        r0 = rev["name"]
        candidate = app["name"] + "--" + suffix
        self.receipt.update(r0=r0, candidate=candidate)
        self.gate.require()
        self.step("dts-gate", observed=self.gate.observed.isoformat())
        payload = build_payload(
            app, rev, resource_id=app_id, stage=stage, service=service, suffix=suffix,
            existing_revisions=revision_rows(self.arm, app_id, self.now()), **inputs,
        )
        self.gate.require_fresh()
        self.guarded_patch(service, projection(app), payload, "create")
        try:
            candidate_rev = self.wait_ready(service, candidate)
            after = self.arm.get(app_id, phase="readback")
            desired_template = payload["properties"]["template"]
            if (candidate_rev.get("name") != candidate
                    or not template_matches((candidate_rev.get("properties") or {}).get("template"), desired_template)
                    or not template_matches((after.get("properties") or {}).get("template"), desired_template)):
                raise Refused("candidate-template")
            if not traffic_is(after, r0):
                raise Refused("traffic-drift")
            self.step("candidate-ready", revision=candidate)
            if stage == "C3":
                result = self.smoke(self.arm, app_id, candidate)
                self.receipt["smoke"] = result
                if not smoke_passed(result):
                    raise Refused("smoke")
                self.step("smoke-passed", calls=len(result["calls"]))
        except Uncertain:
            raise
        except Refused:
            self.deactivate(service, candidate, "deactivate-unpromoted", r0)
            raise
        self.gate.require_fresh()
        self.step("dts-gate", observed=self.gate.observed.isoformat())
        current = self.arm.get(app_id, phase="reread")
        candidate_rev = self.arm.get(f"{app_id}/revisions/{candidate}", phase="reread")
        if (projection(current) != projection(after) or not traffic_is(current, r0)
                or not ready(candidate_rev) or candidate_rev.get("name") != candidate
                or not template_matches((candidate_rev.get("properties") or {}).get("template"), desired_template)):
            raise Refused("concurrent-change")
        self.step("promote", write="PATCH", revision=candidate)
        self.arm.write("PATCH", app_id, {"location": current["location"], "properties": {
            "configuration": {"ingress": {"traffic": [{"revisionName": candidate, "weight": 100}]}}}},
            phase="promote")
        try:
            after = self.arm.get(app_id, phase="readback")
            rev_after = self.arm.get(f"{app_id}/revisions/{candidate}", phase="readback")
            if not traffic_is(after, candidate) or not ready(rev_after):
                raise Refused("promote-readback")
        except Refused:
            # Only the qualified gateway-off release can be restored automatically.
            if stage != "C3":
                raise Uncertain("promote-readback") from None
            restored = self.arm.get(app_id, phase="reconcile")
            if traffic_is(restored, r0):
                raise Refused("promote-readback") from None
            if (not traffic_is(restored, candidate)
                    or not gateway_off_release(self.arm.get(f"{app_id}/revisions/{r0}", phase="reconcile"))):
                raise Uncertain("restore-target") from None
            try:
                self.gate.require_fresh()
                same = projection(self.arm.get(app_id, phase="reread")) == projection(restored)
                qualified = gateway_off_release(self.arm.get(f"{app_id}/revisions/{r0}", phase="reread"))
            except Refused as e:
                raise Uncertain("restore-gate", e.status) from None
            if not same or not qualified:
                raise Uncertain("restore-target") from None
            self.step("restore-traffic", write="PATCH", revision=r0)
            self.arm.write("PATCH", app_id, {"location": restored["location"], "properties": {
                "configuration": {"ingress": {"traffic": [{"revisionName": r0, "weight": 100}]}}}},
                phase="restore")
            if not traffic_is(self.arm.get(app_id, phase="readback"), r0):
                raise Uncertain("restore-readback") from None
            raise
        self.step("promoted", revision=candidate)
        if stage == "C2":
            # Pre-release R0 is deactivated (never deleted) and never automatically restored.
            self.deactivate(service, r0, "deactivate-r0", candidate)
        self.receipt["serving"] = candidate

    def deactivate(self, service: str, revision: str, name: str, serving: str) -> None:
        app_id = APPS[service]
        app = self.arm.get(app_id, phase="reread")
        rev = self.arm.get(f"{app_id}/revisions/{revision}", phase="reread")
        if (revision == serving or not traffic_is(app, serving)
                or (rev.get("properties") or {}).get("active") is not True
                or projection(self.arm.get(app_id, phase="reread")) != projection(app)):
            raise Refused("deactivate-target")
        self.step(name, write="POST", revision=revision)
        self.arm.write("POST", f"{app_id}/revisions/{revision}/deactivate", phase=name)
        rev = self.arm.get(f"{app_id}/revisions/{revision}", phase="readback")
        if (rev.get("properties") or {}).get("active") is not False:
            raise Refused(name + "-readback")

    def rollback(self, target: str) -> None:
        app_id = APPS["api"]
        app = self.arm.get(app_id, phase="read")
        if not re.fullmatch(re.escape(app["name"]) + r"--[a-z0-9-]{1,40}", target):
            raise Refused("rollback-target")
        rev = self.arm.get(f"{app_id}/revisions/{target}", phase="read")
        if not gateway_off_release(rev):
            # Only the released gateway-off image is a rollback target; a pre-release
            # revision could reactivate retired work and needs separate qualification.
            raise Refused("rollback-target")
        if traffic_is(app, target):
            self.step("rollback-noop", revision=target)
            return
        self.gate.require()
        self.gate.require_fresh()
        if (projection(self.arm.get(app_id, phase="reread")) != projection(app)
                or not gateway_off_release(self.arm.get(f"{app_id}/revisions/{target}", phase="reread"))):
            raise Refused("rollback-target")
        self.step("rollback", write="PATCH", revision=target)
        self.arm.write("PATCH", app_id, {"location": app["location"], "properties": {
            "configuration": {"ingress": {"traffic": [{"revisionName": target, "weight": 100}]}}}},
            phase="rollback")
        if not traffic_is(self.arm.get(app_id, phase="readback"), target):
            raise Uncertain("rollback-readback")
        self.receipt["serving"] = target
        self.step("rolled-back", revision=target)

    def run(self, stage: str) -> None:
        if stage in ("c1-web", "c1-api"):
            self.c1(stage[3:])
        elif stage in ("c2-web", "c2-api"):
            service = stage[3:]
            self.create_and_promote(service, "C2", api_image=IMAGES["api"] if service == "api" else "",
                                    web_image=IMAGES["web"] if service == "web" else "")
        elif stage == "c3-api":
            _, rev = self.snapshot("api")
            containers = rev["properties"]["template"]["containers"]
            if len(containers) != 1 or containers[0].get("image") != IMAGES["api"]:
                raise Refused("c3-baseline")
            self.create_and_promote(
                "api", "C3", manifest_bytes=load_manifest(self.env),
                classification_sidecar_bytes=load_sidecar(self.env),
                deployments_bytes=DEPLOYMENTS_BYTES, deployments_sha256=DEPLOYMENTS_SHA256)
        elif stage == "rollback-api":
            self.rollback(self.env["AI4IA_ROLLBACK_REVISION"])
        else:
            raise Refused("admission")


def reconcile(arm: Arm | None, stage: str | None) -> dict | None:
    """Observe (never repeat) after an uncertain write: names and weights only."""
    if arm is None or not stage:
        return None
    service = "web" if stage.endswith("-web") else "api"
    try:
        app = arm.get(APPS[service], phase="reconcile")
        props = app.get("properties") or {}
        rows = revision_rows(arm, APPS[service], datetime.now(UTC))["rows"]
    except Exception:  # noqa: BLE001 - observation only; any failure is reported as unobserved
        return {"observed": False}
    return {
        "observed": True,
        "mode": (props.get("configuration") or {}).get("activeRevisionsMode"),
        "latest": props.get("latestRevisionName"),
        "traffic": [{"revisionName": t.get("revisionName"), "weight": t.get("weight")}
                    for t in ((props.get("configuration") or {}).get("ingress") or {}).get("traffic") or []
                    if isinstance(t, dict)],
        "active": sorted(r["name"] for r in rows if r.get("active") is True and isinstance(r.get("name"), str)),
    }


def execute(env: dict[str, str], *, token_source=acquire_arm_token, arm_class=Arm,
            now=lambda: datetime.now(UTC)) -> dict:
    receipt: dict[str, Any] = {"stage": None, "outcome": "refused", "phase": "admission",
                               "status": None, "steps": []}
    arm = None
    try:
        stage = validate_environment(env)
        receipt.update(stage=stage, sha=env["GITHUB_SHA"], run=env["GITHUB_RUN_ID"],
                       deployments_sha256=DEPLOYMENTS_SHA256)
        if stage == "c3-api":
            receipt.update(manifest_sha256=hashlib.sha256(load_manifest(env)).hexdigest(),
                           sidecar_sha256=hashlib.sha256(load_sidecar(env)).hexdigest())
        receipt["phase"] = "credential"
        arm = arm_class(token_source(env, time.time()))
        receipt["phase"] = "stage"
        Release(arm, env, receipt, now=now).run(stage)
        receipt.update(outcome="complete", phase="complete")
    except Uncertain as e:
        receipt.update(outcome="uncertain", phase=e.phase, status=e.status)
        receipt["reconciled"] = reconcile(arm, receipt.get("stage"))
    except Refused as e:
        receipt.update(phase=e.phase, status=e.status)
    except Exception as e:  # noqa: BLE001 - receipt records only the exception type
        receipt.update(outcome="error", error=type(e).__name__)
    receipt["writes"] = arm.writes if arm is not None else 0
    return receipt


def main() -> int:
    receipt = execute(dict(os.environ))
    text = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    target = os.environ.get("RUNNER_TEMP")
    if target:
        Path(target, "gateway-release-receipt.json").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if receipt["outcome"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
