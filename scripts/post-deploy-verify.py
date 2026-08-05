#!/usr/bin/env python3
"""Post-deploy proof and revision rollback for the AI4IA deploy workflow.

Why this exists
---------------
`azd deploy` succeeding means "the image built, pushed, and ARM accepted a new
Container App template". It does **not** mean the app runs. A bad image, an
unbound or misbound secret, a Python import error, a broken APIM route, or a
gateway policy that no longer matches the catalog all finish as a GREEN deploy,
and the first person to notice is a user. `azure.yaml`'s postprovision hook runs
*before* application deployment and deliberately treats app probes as
best-effort for exactly that reason (the api container may still be the azd
placeholder image at that point), so it cannot close this gap either.

This script is the enforced gate that runs *after* `azd deploy`:

``capture``   Record, per Container App, the revision that is currently taking
              traffic (plus its image, revision mode, and min-replica setting).
              Run this BEFORE `azd deploy`; it is the rollback target.

``verify``    Assert the deploy actually landed and the app actually serves:
              rollout, API live/ready, web root, model-proxy ingress, custom
              domain bindings, and one authenticated canary that traverses the
              real governed path FastAPI -> SimpleL7Proxy -> APIM -> Foundry.

``rollback``  Restore the captured revision for every app whose active revision
              moved. Run this only when ``verify`` failed.

Design notes
------------
* **No azd state.** Resource group and app names are derived the same way
  `deploy.yml`'s custom-domain preflight derives them, and FQDNs are read back
  from `az containerapp show`. `.azure/` is gitignored, so a `workflow_dispatch`
  run that skips provisioning has no azd outputs to read.
* **No hardcoded deployment names.** The canary picks a model id from
  `infra/models.json` (the catalog source of truth) and intersects it with the
  models the live API says it will actually route to, so a residency policy or a
  regional outage produces a clear message instead of a mystery 400.
* **Nothing is ever printed unredacted.** Bearer tokens, JWTs, `?api-key=`
  style query values and `key: value` secret shapes are scrubbed from every
  line, matching `scripts/voice-live-canary.py`. The canary never prints the
  model's reply, only its length.

Exit codes: 0 ok, 2 usage/configuration error, 3 verification failed,
4 rollback failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

STATE_VERSION = 1
SERVICES = ("api", "web", "proxy")
APP_NAME_PREFIX = {"api": "ca-api-", "web": "ca-web-", "proxy": "ca-proxy-"}
DEFAULT_WORKLOAD = "ai4ia"
DEFAULT_TOKEN_ENV = "AI4IA_DEPLOY_CANARY_TOKEN"

# Container Apps' own edge returns these when nothing behind the ingress is
# serving yet -- which is precisely the scale-to-zero cold start we must retry
# rather than fail on.
EDGE_FAILURE_STATUSES = frozenset({502, 503, 504})
RETRYABLE_STATUSES = frozenset({429}) | EDGE_FAILURE_STATUSES

HEALTHY_HEALTH_STATES = frozenset({"healthy"})
RUNNING_STATES = frozenset({"running", "runningatmaxscale"})
# A minReplicas=0 app that has scaled down is *correct*, not broken. The HTTP
# probe is what proves it can still serve.
IDLE_RUNNING_STATES = frozenset({"scaledtozero", "scaleddown", "stopped"})

MAX_SAFE_CHARS = 512
MAX_BODY_BYTES = 64 * 1024

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENVIRONMENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,60}\Z", re.IGNORECASE)
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Same redaction contract as scripts/voice-live-canary.py. Keep them in step:
# both write operator-facing output into a CI log that is retained.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|sig|code|subscription[_-]?key)=)[^&#\s]+",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    # The label alternation covers the credential names this stack actually
    # carries, not just generic ones: APIM's `Ocp-Apim-Subscription-Key`, the
    # proxy's `S7P-KEY`, and the plain `subscription-key` form all appear in
    # gateway error bodies, and none of them contains the word "key" in a shape
    # `api[_-]?key` matches.
    #
    # The optional quote before the separator is the difference from the voice
    # canary's copy, and it is load-bearing here: this script decodes JSON error
    # bodies from the API and the gateway, where a leaked credential arrives as
    # `"api_key": "..."` -- and `\s*[:=]` alone does not match across the closing
    # quote of the key. Do not "unify" these by dropping it.
    r"(\b(?:api[_-]?key|x-api-key|access[_-]?token|token|authorization|secret|password"
    r"|ocp-apim-subscription-key|subscription[_-]?key|s7p[_-]?key)"
    r"\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)


class VerifyInputError(ValueError):
    """The operator supplied an unsafe or unusable input."""


class AzError(RuntimeError):
    """An `az` invocation failed or returned something unparseable."""


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def redact(value: object, *, max_chars: int = MAX_SAFE_CHARS) -> str | None:
    """Scrub credentials out of anything on its way to the log."""

    if value is None:
        return None
    safe = value if isinstance(value, str) else str(value)
    safe = safe[:8192]
    safe = _CONTROL_RE.sub(" ", safe)
    safe = _BEARER_RE.sub("******", safe)
    safe = _JWT_RE.sub("[REDACTED]", safe)
    safe = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", safe)
    safe = _SECRET_VALUE_RE.sub(r"\1[REDACTED]", safe)
    safe = " ".join(safe.split()).strip()[: max(0, max_chars)]
    return safe or None


def _redact_deep(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v) for v in value]
    return value


def emit(event: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"event": event}
    payload.update({k: _redact_deep(v) for k, v in fields.items() if v is not None})
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=True), flush=True)


def annotate(level: str, message: str) -> None:
    """GitHub workflow annotation. Safe (and quiet) outside Actions."""

    safe = redact(message) or "unspecified"
    print(f"::{level}::{safe}", flush=True)


# --------------------------------------------------------------------------
# az
# --------------------------------------------------------------------------


def _az_binary() -> str:
    override = os.environ.get("AI4IA_AZ_CLI")
    if override:
        return override
    return shutil.which("az") or "az"


def run_az(args: Sequence[str], *, timeout: float = 180.0) -> tuple[int, str, str]:
    """Invoke `az`. Indirected through a module attribute so tests can stub it."""

    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
            [_az_binary(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment problem
        raise AzError("the az CLI is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - slow ARM
        raise AzError("az timed out") from exc
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def az_json(args: Sequence[str]) -> Any:
    code, out, err = run_az([*args, "-o", "json"])
    if code != 0:
        raise AzError(redact(err) or redact(out) or "az exited non-zero")
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError as exc:
        raise AzError("az returned output that is not JSON") from exc


# --------------------------------------------------------------------------
# Container App shape readers (pure)
# --------------------------------------------------------------------------


def _properties(app: Any) -> dict:
    if not isinstance(app, dict):
        return {}
    props = app.get("properties")
    return props if isinstance(props, dict) else {}


def traffic_revision(app: Any) -> str | None:
    """The revision currently receiving traffic.

    Both revision modes have to work. In ``Single`` mode ARM reports
    ``traffic: [{latestRevision: true, weight: 100}]`` with no revision name, so
    the name has to come from ``latestReadyRevisionName``. In ``Multiple`` mode
    the targets are named and weighted, and the answer is the heaviest one --
    reading ``latestReadyRevisionName`` there would name a revision that may be
    receiving no traffic at all.
    """

    props = _properties(app)
    config = props.get("configuration")
    config = config if isinstance(config, dict) else {}
    ingress = config.get("ingress")
    ingress = ingress if isinstance(ingress, dict) else {}
    targets = ingress.get("traffic")
    targets = targets if isinstance(targets, list) else []

    latest = props.get("latestReadyRevisionName")
    # Deliberately NOT falling back to `latestRevisionName`: that names the most
    # recently CREATED revision, ready or not. Using it would let a revision that
    # never became ready be reported as the one serving traffic -- and, worse,
    # be captured as a rollback target.
    latest = latest if isinstance(latest, str) and latest else None

    best: str | None = None
    best_weight = 0
    for target in targets:
        if not isinstance(target, dict):
            continue
        weight = target.get("weight")
        weight = weight if isinstance(weight, int) else 0
        if weight <= 0:
            # A drained target is not serving. Treating it as the active revision
            # would report a rollout as healthy while the app answers nothing.
            continue
        name = target.get("revisionName")
        if target.get("latestRevision") is True:
            name = latest
        if not isinstance(name, str) or not name:
            continue
        if weight > best_weight:
            best, best_weight = name, weight
    if targets:
        return best
    # No traffic block at all: an app that has never had ingress configured, or
    # an ARM shape this does not recognise. Fall back to the latest ready
    # revision rather than claiming nothing is serving.
    return latest


def revisions_mode(app: Any) -> str:
    config = _properties(app).get("configuration")
    config = config if isinstance(config, dict) else {}
    mode = config.get("activeRevisionsMode")
    return mode if isinstance(mode, str) and mode else "Single"


def min_replicas(app: Any) -> int | None:
    template = _properties(app).get("template")
    template = template if isinstance(template, dict) else {}
    scale = template.get("scale")
    scale = scale if isinstance(scale, dict) else {}
    value = scale.get("minReplicas")
    return value if isinstance(value, int) else None


def container_image(app: Any) -> str | None:
    template = _properties(app).get("template")
    template = template if isinstance(template, dict) else {}
    containers = template.get("containers")
    if not isinstance(containers, list):
        return None
    for container in containers:
        if isinstance(container, dict) and isinstance(container.get("image"), str):
            return container["image"]
    return None


def ingress_fqdn(app: Any) -> str | None:
    config = _properties(app).get("configuration")
    config = config if isinstance(config, dict) else {}
    ingress = config.get("ingress")
    ingress = ingress if isinstance(ingress, dict) else {}
    fqdn = ingress.get("fqdn")
    return fqdn if isinstance(fqdn, str) and fqdn else None


def bound_custom_domains(app: Any) -> dict[str, str]:
    """Map hostname -> bindingType for every custom domain on the ingress."""

    config = _properties(app).get("configuration")
    config = config if isinstance(config, dict) else {}
    ingress = config.get("ingress")
    ingress = ingress if isinstance(ingress, dict) else {}
    domains = ingress.get("customDomains")
    if not isinstance(domains, list):
        return {}
    bound: dict[str, str] = {}
    for domain in domains:
        if isinstance(domain, dict) and isinstance(domain.get("name"), str):
            binding = domain.get("bindingType")
            bound[domain["name"]] = binding if isinstance(binding, str) else "Unknown"
    return bound


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AppSnapshot:
    service: str
    name: str
    exists: bool
    revision: str | None = None
    revisionsMode: str = "Single"
    minReplicas: int | None = None
    image: str | None = None
    fqdn: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "name": self.name,
            "exists": self.exists,
            "revision": self.revision,
            "revisionsMode": self.revisionsMode,
            "minReplicas": self.minReplicas,
            "image": self.image,
            "fqdn": self.fqdn,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> AppSnapshot:
        if not isinstance(raw, dict):
            raise VerifyInputError("state file contains a malformed app entry")
        try:
            return cls(
                service=str(raw["service"]),
                name=str(raw["name"]),
                exists=bool(raw.get("exists", True)),
                revision=raw.get("revision"),
                revisionsMode=str(raw.get("revisionsMode") or "Single"),
                minReplicas=raw.get("minReplicas"),
                image=raw.get("image"),
                fqdn=raw.get("fqdn"),
            )
        except KeyError as exc:
            raise VerifyInputError(f"state file app entry is missing {exc}") from exc


@dataclass
class DeployState:
    resourceGroup: str
    apps: list[AppSnapshot] = field(default_factory=list)
    version: int = STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "resourceGroup": self.resourceGroup,
            "apps": [app.to_dict() for app in self.apps],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DeployState:
        if not isinstance(raw, dict):
            raise VerifyInputError("state file is not a JSON object")
        version = raw.get("version")
        if version != STATE_VERSION:
            raise VerifyInputError(
                f"state file version {version!r} is not supported (expected {STATE_VERSION})"
            )
        group = raw.get("resourceGroup")
        if not isinstance(group, str) or not group:
            raise VerifyInputError("state file has no resourceGroup")
        apps = raw.get("apps")
        if not isinstance(apps, list):
            raise VerifyInputError("state file has no apps list")
        return cls(resourceGroup=group, apps=[AppSnapshot.from_dict(a) for a in apps])

    def app(self, service: str) -> AppSnapshot | None:
        return next((a for a in self.apps if a.service == service), None)


def resource_group_name(workload: str | None, environment: str) -> str:
    """Mirror infra/main.bicep's ``rg-${workload}-${environmentName}``."""

    token = (workload or "").strip() or DEFAULT_WORKLOAD
    return f"rg-{token}-{environment}"


def app_names(environment: str) -> dict[str, str]:
    return {svc: f"{APP_NAME_PREFIX[svc]}{environment}" for svc in SERVICES}


def _validate_environment(value: str | None) -> str:
    candidate = (value or "").strip()
    if not _ENVIRONMENT_RE.fullmatch(candidate):
        raise VerifyInputError(
            "AZURE_ENV_NAME must be a simple environment token (letters, digits, hyphens)."
        )
    return candidate


_NOT_FOUND_MARKERS = (
    "resourcenotfound",
    "was not found",
    "could not be found",
    "not found",
    "does not exist",
)


def _is_not_found(message: str) -> bool:
    """Distinguish "this app does not exist yet" from "the read failed".

    Everything downstream turns on this. Treating a 403, a throttle, or a network
    blip as "absent" is the worst outcome available at capture time: it silently
    drops the unchanged-revision assertion AND the rollback target, so the gate
    reports a clean run over a deploy it never actually checked.
    """

    lowered = message.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def snapshot_app(
    resource_group: str,
    service: str,
    name: str,
    *,
    attempts: int = 3,
    delay: float = 5.0,
    sleep: Callable[[float], None] | None = None,
) -> AppSnapshot:
    """Read one app's pre-deploy state, or raise.

    Raising is correct here: capture runs BEFORE `azd deploy`, so failing costs
    nothing but a re-run, whereas guessing costs the whole gate.
    """

    do_sleep = sleep or time.sleep
    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            app = az_json(["containerapp", "show", "-g", resource_group, "-n", name])
        except AzError as exc:
            last_error = str(exc)
            if _is_not_found(last_error):
                return AppSnapshot(service=service, name=name, exists=False)
        else:
            if not isinstance(app, dict):
                return AppSnapshot(service=service, name=name, exists=False)
            return AppSnapshot(
                service=service,
                name=name,
                exists=True,
                revision=traffic_revision(app),
                revisionsMode=revisions_mode(app),
                minReplicas=min_replicas(app),
                image=container_image(app),
                fqdn=ingress_fqdn(app),
            )
        if attempt < attempts:
            do_sleep(delay)
    raise VerifyInputError(
        f"could not read {name} in {resource_group} ({last_error}). Capture runs before "
        "the deploy, so this fails now rather than silently disabling the rollback target."
    )


# --------------------------------------------------------------------------
# rollout assertions (pure)
# --------------------------------------------------------------------------


def _revision_props(revision: Any) -> dict:
    props = _properties(revision)
    return props if props else (revision if isinstance(revision, dict) else {})


def rollout_problems(
    *,
    service: str,
    previous_revision: str | None,
    current_revision: str | None,
    revision_detail: Any,
    require_replicas: bool,
    previous_image: str | None = None,
    current_image: str | None = None,
) -> list[str]:
    """Everything wrong with this app's rollout, in operator-facing English.

    An empty list means the new revision exists, is active, reports Healthy, and
    is running. The revision-name comparison is the assertion that catches the
    failure this whole gate was written for: `azd deploy` reporting success
    without Container Apps ever promoting a new template.
    """

    problems: list[str] = []
    if not current_revision:
        problems.append(f"{service}: no revision is receiving traffic")
        return problems
    if previous_revision and current_revision == previous_revision:
        problems.append(
            f"{service}: still serving the pre-deploy revision {current_revision} -- "
            "azd reported success but Container Apps never promoted a new template"
        )
    elif previous_image and current_image and previous_image == current_image:
        # A NEW revision running the OLD image. Capture happens after
        # `azd provision`, and `azd deploy` tags every build uniquely, so the
        # image must move; an unchanged one means the revision was created by
        # something other than this deploy's push.
        problems.append(
            f"{service}: revision {current_revision} is new but still runs the "
            "pre-deploy image -- the deploy did not replace the container"
        )

    props = _revision_props(revision_detail)
    if not props:
        problems.append(f"{service}: revision {current_revision} returned no state")
        return problems

    if props.get("active") is False:
        problems.append(f"{service}: revision {current_revision} is not active")

    health = props.get("healthState")
    health_key = health.lower() if isinstance(health, str) else ""
    if health_key and health_key not in HEALTHY_HEALTH_STATES:
        problems.append(
            f"{service}: revision {current_revision} healthState is {health} (expected Healthy)"
        )

    running_state = props.get("runningState")
    running_key = running_state.lower() if isinstance(running_state, str) else ""
    replicas = props.get("replicas")
    replicas = replicas if isinstance(replicas, int) else None

    if running_key and running_key not in RUNNING_STATES:
        # Scaled to zero is only acceptable for an app configured to allow it;
        # the HTTP probe is what proves such an app can still serve.
        if not (running_key in IDLE_RUNNING_STATES and not require_replicas):
            problems.append(
                f"{service}: revision {current_revision} runningState is {running_state}"
            )

    if require_replicas and (replicas is None or replicas <= 0):
        problems.append(
            f"{service}: revision {current_revision} has {replicas if replicas is not None else 'no'} "
            "running replicas (min replicas is 1 or more)"
        )
    return problems


def rollback_commands(
    *, resource_group: str, snapshot: AppSnapshot, current_revision: str | None
) -> list[list[str]]:
    """The `az` argv needed to put ``snapshot.revision`` back in front of traffic.

    Two shapes, because the primitive differs by revision mode and getting it
    wrong is a silent no-op:

    * ``Multiple`` -- shift the weights back; the old revision is still live.
    * ``Single``   -- ``ingress traffic set`` is rejected outright, so the
      supported move is ``revision copy``, which clones the captured revision's
      whole template (image digest, env, secret refs) into a new active
      revision.

    Returns an empty list when there is nothing to undo: the app never existed,
    it had no captured revision (a greenfield first deploy has nothing to roll
    back TO), or it is already serving the captured revision.
    """

    if not snapshot.exists or not snapshot.revision:
        return []
    if current_revision and current_revision == snapshot.revision:
        return []
    if snapshot.revisionsMode.strip().lower() == "multiple":
        return [
            [
                "containerapp",
                "ingress",
                "traffic",
                "set",
                "-g",
                resource_group,
                "-n",
                snapshot.name,
                "--revision-weight",
                f"{snapshot.revision}=100",
            ]
        ]
    return [
        [
            "containerapp",
            "revision",
            "copy",
            "-g",
            resource_group,
            "-n",
            snapshot.name,
            "--from-revision",
            snapshot.revision,
        ]
    ]


# --------------------------------------------------------------------------
# HTTP probing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpOutcome:
    status: int | None
    body: bytes = b""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError:
            return None


def validate_https_base(value: str, *, label: str) -> str:
    parsed = urlsplit(value.strip())
    try:
        parsed.port
    except ValueError as exc:
        raise VerifyInputError(f"{label} port is invalid.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.netloc.isascii()
        or _CONTROL_RE.search(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VerifyInputError(f"{label} must be a credential-free https:// base URL.")
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", parsed.netloc, path, "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the 3xx instead of following it.

    ``urllib`` copies the request headers onto a redirect, so following one would
    replay the canary's ``Authorization`` header at whatever host the response
    named -- a credential handed to an arbitrary origin on the say-so of a
    server we are in the middle of deciding we do not trust. None of the probed
    endpoints should redirect, so the 3xx is surfaced as the answer instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class Deadline:
    """A single wall-clock budget for the whole verification run.

    Per-check retry budgets multiply: the configured attempts x delays across
    three apps, four HTTP targets and the canary add up to considerably more
    than the step timeout, so a run where several checks are slow could be killed
    by the step timeout mid-flight rather than finishing and reporting. One
    shared deadline bounds the total instead, and every check reports what it
    last saw when the budget runs out.
    """

    seconds: float
    clock: Callable[[], float] = time.monotonic
    started: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def expired(self) -> bool:
        return self.seconds > 0 and (self.clock() - self.started) >= self.seconds

    def remaining(self) -> float:
        if self.seconds <= 0:
            return float("inf")
        return max(0.0, self.seconds - (self.clock() - self.started))


def http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> HttpOutcome:
    """One request. Never raises, never leaks the request into the message.

    Handshake/transport exceptions routinely stringify the full URL and the
    request headers, which is where an ``Authorization`` value would surface, so
    only the exception's class name is ever reported.
    """

    request = urllib.request.Request(url, method=method, data=body)  # noqa: S310 - https validated
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return HttpOutcome(status=response.status, body=response.read(MAX_BODY_BYTES))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001 - body is best-effort diagnostics only
            payload = b""
        return HttpOutcome(status=exc.code, body=payload)
    except Exception as exc:  # noqa: BLE001 - see docstring
        return HttpOutcome(status=None, error=type(exc).__name__)


def is_retryable(outcome: HttpOutcome) -> bool:
    """Cold start and edge churn are retryable; a real 4xx is an answer.

    The model proxy can be configured with ``minReplicas=0``, so the first
    request after a deploy may spend 10-30s waking a replica and surface as a
    transport timeout or a Container Apps 503. Treating that as a failure would
    make the gate flaky, which is the fastest way to get a gate switched off.
    """

    if outcome.status is None:
        return True
    if outcome.status in RETRYABLE_STATUSES:
        return True
    return outcome.status >= 500


def probe(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    accept: Callable[[HttpOutcome], bool] | None = None,
    attempts: int = 10,
    delay: float = 5.0,
    timeout: float = 25.0,
    request: Callable[..., HttpOutcome] | None = None,
    sleep: Callable[[float], None] | None = None,
    deadline: Deadline | None = None,
) -> tuple[HttpOutcome, int]:
    """Retry ``url`` until it is acceptable, the attempts run out, or time is up."""

    do_request = request or http_request
    do_sleep = sleep or time.sleep
    accepts = accept or (lambda outcome: outcome.ok)
    outcome = HttpOutcome(status=None, error="not attempted")
    attempt = 0
    for attempt in range(1, max(1, attempts) + 1):
        outcome = do_request(
            method, url, headers=headers, body=body, timeout=timeout
        )
        if accepts(outcome):
            return outcome, attempt
        if not is_retryable(outcome) or attempt >= attempts:
            return outcome, attempt
        if deadline is not None and deadline.expired():
            return outcome, attempt
        do_sleep(delay)
    return outcome, attempt


def ingress_responds(outcome: HttpOutcome) -> bool:
    """Proof that *something in the container* answered, and answered sanely.

    Deliberately not ``status == 200``. The model proxy is an authenticating
    gateway: an unauthenticated probe legitimately gets 401/404, and both prove
    the revision booted and is serving.

    But every 5xx is rejected, not just Container Apps' own 502/503/504. The
    proxy's probe routes are defined to answer 200 or 503, so a 500 from one is a
    fault inside the container, and accepting it would pass a proxy that boots
    and then fails every request. 503 in particular must stay in the retryable
    set: it is what ``/startup`` returns while a cold replica is still coming up.
    """

    return outcome.status is not None and outcome.status < 500


def ingress_or_redirect(outcome: HttpOutcome) -> bool:
    """Web-root acceptance: 2xx or a redirect, but never a followed one.

    Redirects are not followed (see ``_OPENER``), so a framework that answers the
    root with a 307/308 to a locale or landing route would otherwise read as a
    failure. The redirect itself still proves the server rendered a decision.
    """

    return outcome.status is not None and 200 <= outcome.status < 400


# --------------------------------------------------------------------------
# canary
# --------------------------------------------------------------------------

CANARY_PROMPT = "Reply with the single word: ready."
CANARY_TITLE = "post-deploy canary"
# Cheapest first. Only conversational categories -- a capability model (image,
# tts, embedding) is not reachable through /api/chat at all.
CANARY_CATEGORY_ORDER = ("chat-fast", "chat", "reasoning")


def catalog_model_preferences(catalog_doc: Any) -> list[str]:
    """Candidate chat model ids from infra/models.json, cheapest first.

    Deterministic on purpose: a canary that picks a different model per run
    tests a different path per run. Never returns a *deployment* name -- the
    deployment is derived server-side from the catalog, and hardcoding one here
    would be the exact governance violation AGENTS.md forbids.
    """

    catalog = catalog_doc.get("catalog") if isinstance(catalog_doc, dict) else None
    if not isinstance(catalog, list):
        return []
    by_category: dict[str, list[str]] = {}
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        category = entry.get("category")
        if not isinstance(name, str) or not _MODEL_ID_RE.fullmatch(name):
            continue
        if category not in CANARY_CATEGORY_ORDER:
            continue
        if not isinstance(entry.get("deployments"), list) or not entry["deployments"]:
            continue
        by_category.setdefault(category, []).append(name)
    ordered: list[str] = []
    for category in CANARY_CATEGORY_ORDER:
        ordered.extend(sorted(set(by_category.get(category, []))))
    return ordered


def select_canary_model(catalog_doc: Any, available_ids: Sequence[str]) -> str:
    """First catalog preference the live API says it will actually route to."""

    available = {i for i in available_ids if isinstance(i, str)}
    if not available:
        raise VerifyInputError("the API advertises no models; nothing to canary")
    for candidate in catalog_model_preferences(catalog_doc):
        if candidate in available:
            return candidate
    raise VerifyInputError(
        "no conversational model in infra/models.json is advertised by the deployed API "
        "(check the data-residency policy and the model deployments)"
    )


def load_model_catalog(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VerifyInputError(f"cannot read {path.name}") from exc
    except ValueError as exc:
        raise VerifyInputError(f"{path.name} is not valid JSON") from exc


@dataclass
class CanaryResult:
    ok: bool
    model: str | None = None
    detail: str | None = None
    reply_chars: int | None = None
    elapsed_ms: int | None = None


def run_canary(
    *,
    api_base: str,
    token: str,
    catalog_doc: Any,
    model_override: str | None = None,
    attempts: int = 3,
    delay: float = 10.0,
    timeout: float = 120.0,
    request: Callable[..., HttpOutcome] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    deadline: Deadline | None = None,
) -> CanaryResult:
    """One authenticated turn down the whole governed path.

    FastAPI authenticates the bearer, resolves the deployment from the catalog,
    and calls the model gateway -- which is the SimpleL7Proxy ingress, which
    fronts APIM, which fronts Foundry. A non-empty completed reply is therefore
    proof of every hop, including the APIM subscription keys and the gateway
    policy, none of which any other assertion here touches.

    The reply text is never printed. Only its length is, which is all that is
    needed to distinguish "the model answered" from "the model returned empty".
    """

    do_request = request or http_request
    clock = monotonic or time.monotonic
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    json_headers = {**auth, "Content-Type": "application/json"}
    started = clock()

    models, _ = probe(
        f"{api_base}/api/models",
        headers=auth,
        attempts=attempts,
        delay=delay,
        timeout=min(timeout, 60.0),
        request=do_request,
        sleep=sleep,
        deadline=deadline,
    )
    if not models.ok:
        return CanaryResult(
            ok=False,
            detail=_http_detail("GET /api/models", models),
        )
    payload = models.json()
    advertised = []
    if isinstance(payload, dict) and isinstance(payload.get("models"), list):
        advertised = [m.get("id") for m in payload["models"] if isinstance(m, dict)]

    try:
        model = model_override or select_canary_model(catalog_doc, advertised)
    except VerifyInputError as exc:
        return CanaryResult(ok=False, detail=str(exc))
    if not _MODEL_ID_RE.fullmatch(model):
        return CanaryResult(ok=False, detail="canary model id is not a simple identifier")

    created, _ = probe(
        f"{api_base}/api/sessions",
        method="POST",
        headers=json_headers,
        body=json.dumps({"title": CANARY_TITLE, "model": model}).encode("utf-8"),
        accept=lambda outcome: outcome.status == 201,
        attempts=attempts,
        delay=delay,
        timeout=min(timeout, 60.0),
        request=do_request,
        sleep=sleep,
        deadline=deadline,
    )
    if created.status != 201:
        return CanaryResult(
            ok=False, model=model, detail=_http_detail("POST /api/sessions", created)
        )
    session = created.json()
    session_id = session.get("id") if isinstance(session, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return CanaryResult(ok=False, model=model, detail="session create returned no id")

    try:
        chat, _ = probe(
            f"{api_base}/api/chat",
            method="POST",
            headers=json_headers,
            # No `params`. A small `max_tokens` looks like a cheap safeguard but
            # is a trap: the cheapest catalog models are reasoning models, and a
            # tight cap gets spent on reasoning tokens, yielding an empty
            # completion and a canary that fails a perfectly healthy deploy. The
            # per-model output cap is applied server-side anyway, and the prompt
            # is what keeps the reply to one word.
            body=json.dumps(
                {
                    "sessionId": session_id,
                    "content": CANARY_PROMPT,
                    "model": model,
                    "stream": False,
                }
            ).encode("utf-8"),
            attempts=attempts,
            delay=delay,
            timeout=timeout,
            request=do_request,
            sleep=sleep,
            deadline=deadline,
        )
        elapsed_ms = int((clock() - started) * 1000)
        if not chat.ok:
            return CanaryResult(
                ok=False,
                model=model,
                elapsed_ms=elapsed_ms,
                detail=_http_detail("POST /api/chat", chat),
            )
        answer = chat.json()
        message = answer.get("message") if isinstance(answer, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        status = message.get("status") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            return CanaryResult(
                ok=False,
                model=model,
                elapsed_ms=elapsed_ms,
                detail="the model returned an empty reply",
            )
        if status not in (None, "complete"):
            return CanaryResult(
                ok=False,
                model=model,
                elapsed_ms=elapsed_ms,
                reply_chars=len(content),
                detail=f"the assistant message status is {status}",
            )
        return CanaryResult(
            ok=True, model=model, elapsed_ms=elapsed_ms, reply_chars=len(content)
        )
    finally:
        # The canary owns this session, so it cleans it up. A failed cleanup is
        # reported but never fails the deploy: one stray session is a smaller
        # problem than rolling back a healthy release.
        deleted = do_request(
            "DELETE",
            f"{api_base}/api/sessions/{session_id}",
            headers=auth,
            body=None,
            timeout=min(timeout, 60.0),
        )
        if deleted.status not in (200, 202, 204, 404):
            emit(
                "canary_cleanup_failed",
                status=deleted.status,
                error=deleted.error,
            )


def _http_detail(label: str, outcome: HttpOutcome) -> str:
    if outcome.status is None:
        return f"{label} did not complete ({outcome.error or 'transport error'})"
    snippet = redact(outcome.body[:512].decode("utf-8", "replace"), max_chars=200)
    return f"{label} returned HTTP {outcome.status}" + (f": {snippet}" if snippet else "")


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------


def cmd_capture(args: argparse.Namespace) -> int:
    environment = _validate_environment(args.environment)
    group = args.resource_group or resource_group_name(args.workload, environment)
    names = app_names(environment)
    snapshots = [snapshot_app(group, svc, names[svc]) for svc in SERVICES]
    state = DeployState(resourceGroup=group, apps=snapshots)
    Path(args.state).write_text(
        json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    for snapshot in snapshots:
        emit(
            "captured",
            service=snapshot.service,
            app=snapshot.name,
            exists=snapshot.exists,
            revision=snapshot.revision,
            revisionsMode=snapshot.revisionsMode,
            minReplicas=snapshot.minReplicas,
        )
    return 0


def _load_state(path: str) -> DeployState:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise VerifyInputError(f"cannot read the capture state file at {path}") from exc
    except ValueError as exc:
        raise VerifyInputError("the capture state file is not valid JSON") from exc
    return DeployState.from_dict(raw)


def _base_url(
    live: dict[str, Any],
    snapshot: AppSnapshot | None,
    service: str,
    override: str | None,
    label: str,
) -> str | None:
    """Where to probe this service.

    The LIVE app is the first source, not the capture. On a greenfield first
    deploy the capture recorded ``exists: false`` with no FQDN, and preferring
    the captured value there would fail the very first deploy of a new tenant
    with "no ingress FQDN to probe" while the app was in fact serving fine.
    """

    if override:
        return validate_https_base(override, label=label)
    fqdn = ingress_fqdn(live.get(service)) or (snapshot.fqdn if snapshot else None)
    if fqdn:
        return validate_https_base(f"https://{fqdn}", label=label)
    return None


def await_rollout(
    *,
    resource_group: str,
    service: str,
    snapshot: AppSnapshot,
    attempts: int = 20,
    delay: float = 10.0,
    sleep: Callable[[float], None] | None = None,
    deadline: Deadline | None = None,
) -> tuple[list[str], dict | None, str | None]:
    """Poll the rollout assertions until they pass or the budget runs out.

    Reading this ONCE is the most dangerous false positive available here.
    `azd deploy` returns when ARM has accepted the new template, but
    ``healthState`` and ``replicas`` lag that by seconds to a minute while the
    replica actually starts -- so a single read would fail a perfectly healthy
    deploy and roll it back. Failing a good release is strictly worse than
    passing a bad one, because it turns a working deploy into an outage AND
    teaches everyone to distrust the gate.

    Returns (problems, live app payload, current revision).
    """

    do_sleep = sleep or time.sleep
    problems: list[str] = [f"{service}: rollout was never evaluated"]
    app: dict | None = None
    current: str | None = None
    for attempt in range(1, max(1, attempts) + 1):
        app, current = None, None
        payload: Any = None
        read_error: str | None = None
        try:
            payload = az_json(
                ["containerapp", "show", "-g", resource_group, "-n", snapshot.name]
            )
        except AzError as exc:
            read_error = str(exc)
        if read_error is not None:
            problems = [f"{service}: could not read {snapshot.name} ({read_error})"]
        elif not isinstance(payload, dict):
            # `az containerapp show` exits 0 with a null body for an app that is
            # not there. Without this branch the previous attempt's problem list
            # would be reported for a deleted app.
            problems = [f"{service}: {snapshot.name} does not exist after deploy"]
        else:
            app = payload
            current = traffic_revision(app)
            detail: Any = None
            if current:
                try:
                    detail = az_json(
                        [
                            "containerapp",
                            "revision",
                            "show",
                            "-g",
                            resource_group,
                            "-n",
                            snapshot.name,
                            "--revision",
                            current,
                        ]
                    )
                except AzError:
                    # A revision ARM has not finished materialising reads as
                    # missing; rollout_problems reports it and the next attempt
                    # tries again.
                    detail = None
            configured_min = min_replicas(app)
            problems = rollout_problems(
                service=service,
                previous_revision=snapshot.revision,
                current_revision=current,
                revision_detail=detail,
                require_replicas=bool(configured_min and configured_min > 0),
                previous_image=snapshot.image,
                current_image=container_image(app),
            )
        if not problems:
            return [], app, current
        if attempt < attempts:
            if deadline is not None and deadline.expired():
                problems.append(f"{service}: ran out of verification time budget")
                return problems, app, current
            do_sleep(delay)
    return problems, app, current


def cmd_verify(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    failures: list[str] = []
    deadline = Deadline(seconds=max(0.0, args.deadline_seconds))

    live: dict[str, Any] = {}
    for service in SERVICES:
        snapshot = state.app(service)
        if snapshot is None:
            failures.append(f"{service}: not present in the capture state file")
            continue
        problems, app, current = await_rollout(
            resource_group=state.resourceGroup,
            service=service,
            snapshot=snapshot,
            attempts=args.rollout_attempts,
            delay=args.rollout_delay,
            deadline=deadline,
        )
        if app is not None:
            live[service] = app
        emit(
            "rollout",
            service=service,
            app=snapshot.name,
            previous=snapshot.revision,
            current=current,
            image=container_image(app) if app else None,
            problems=problems or None,
        )
        failures.extend(problems)

    api_base = _base_url(live, state.app("api"), "api", args.api_url, "API URL")
    web_base = _base_url(live, state.app("web"), "web", args.web_url, "web URL")
    proxy_base = _base_url(live, state.app("proxy"), "proxy", args.proxy_url, "proxy URL")

    if api_base is None:
        failures.append("api: no ingress FQDN to probe")
    else:
        for path in ("/health/live", "/health/ready"):
            outcome, tries = probe(
                f"{api_base}{path}",
                attempts=args.attempts,
                delay=args.delay,
                timeout=args.http_timeout,
                deadline=deadline,
            )
            emit(
                "probe",
                target=f"api{path}",
                status=outcome.status,
                attempts=tries,
                error=outcome.error,
            )
            if not outcome.ok:
                failures.append(
                    f"api: GET {path} is not 200 "
                    f"({outcome.status if outcome.status is not None else outcome.error})"
                )

    if web_base is None:
        failures.append("web: no ingress FQDN to probe")
    else:
        outcome, tries = probe(
            f"{web_base}/",
            accept=ingress_or_redirect,
            attempts=args.attempts,
            delay=args.delay,
            timeout=args.http_timeout,
            deadline=deadline,
        )
        emit("probe", target="web/", status=outcome.status, attempts=tries, error=outcome.error)
        if not ingress_or_redirect(outcome):
            failures.append(
                "web: GET / did not render "
                f"({outcome.status if outcome.status is not None else outcome.error})"
            )

    if not args.proxy_path.startswith("/") or any(
        c in args.proxy_path for c in ("?", "#", " ")
    ):
        raise VerifyInputError("--proxy-path must be a plain absolute path")
    if proxy_base is None:
        failures.append("proxy: no ingress FQDN to probe")
    else:
        outcome, tries = probe(
            f"{proxy_base}{args.proxy_path}",
            accept=ingress_responds,
            attempts=args.proxy_attempts,
            delay=args.delay,
            timeout=args.http_timeout,
            deadline=deadline,
        )
        emit(
            "probe",
            target=f"proxy{args.proxy_path}",
            status=outcome.status,
            attempts=tries,
            error=outcome.error,
        )
        if not ingress_responds(outcome):
            failures.append(
                f"proxy: {args.proxy_path} never answered "
                f"({outcome.status if outcome.status is not None else outcome.error}); "
                "the ingress has no serving replica"
            )

    failures.extend(_custom_domain_failures(live))
    failures.extend(_canary_failures(args, api_base, deadline))

    for failure in failures:
        annotate("error", failure)
    emit("verify", outcome="failed" if failures else "passed", failures=len(failures))
    return 3 if failures else 0


def _custom_domain_failures(live: dict[str, Any]) -> list[str]:
    """A provision can wipe a vanity hostname; prove it survived the deploy.

    deploy.yml's preflight refuses to *start* a run that would wipe a binding.
    This is the other half: confirm after the fact that the hostname is still
    bound AND still SNI-enabled, because a certificate that failed to bind
    leaves the domain listed but serving a TLS error.
    """

    checks = (
        ("web", os.environ.get("AI4IA_WEB_CUSTOM_DOMAIN", "")),
        ("proxy", os.environ.get("AI4IA_PROXY_CUSTOM_DOMAIN", "")),
    )
    failures: list[str] = []
    for service, expected in checks:
        expected = expected.strip()
        if not expected:
            continue
        app = live.get(service)
        if app is None:
            continue
        bound = bound_custom_domains(app)
        binding = bound.get(expected)
        emit("custom_domain", service=service, hostname=expected, binding=binding)
        if binding is None:
            failures.append(
                f"{service}: custom domain {expected} is no longer bound after deploy"
            )
        elif binding != "SniEnabled":
            failures.append(
                f"{service}: custom domain {expected} bindingType is {binding} (expected SniEnabled)"
            )
    return failures


def _canary_failures(
    args: argparse.Namespace, api_base: str | None, deadline: Deadline | None = None
) -> list[str]:
    if args.skip_canary:
        emit("canary", outcome="skipped", detail="--skip-canary was passed")
        annotate(
            "warning",
            "The proxy -> APIM -> Foundry canary was skipped; this deploy has no "
            "end-to-end model proof.",
        )
        return []
    if not _ENV_NAME_RE.fullmatch(args.token_env):
        return ["canary: --token-env is not a valid environment variable name"]
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        # Reached only outside CI: deploy.yml refuses to start a run that would
        # arrive here without a token unless AI4IA_DEPLOY_VERIFY_CANARY=false,
        # which takes the --skip-canary branch above instead.
        emit("canary", outcome="skipped", detail=f"{args.token_env} is empty")
        annotate(
            "warning",
            f"{args.token_env} is empty, so the proxy -> APIM -> Foundry canary did not run. "
            "This deploy is verified as far as the ingress and no further.",
        )
        return []
    if api_base is None:
        return ["canary: no API base URL"]

    catalog_doc = load_model_catalog(args.models)
    result = run_canary(
        api_base=api_base,
        token=token,
        catalog_doc=catalog_doc,
        model_override=args.canary_model,
        attempts=args.canary_attempts,
        delay=args.delay,
        timeout=args.canary_timeout,
        deadline=deadline,
    )
    emit(
        "canary",
        outcome="passed" if result.ok else "failed",
        model=result.model,
        replyChars=result.reply_chars,
        elapsedMs=result.elapsed_ms,
        detail=result.detail,
    )
    if result.ok:
        return []
    return [
        "canary: the governed FastAPI -> SimpleL7Proxy -> APIM -> Foundry path failed "
        f"({result.detail or 'no detail'})"
    ]


def confirm_restored(
    *,
    resource_group: str,
    snapshot: AppSnapshot,
    replaced_revision: str | None,
    attempts: int = 12,
    delay: float = 10.0,
    sleep: Callable[[float], None] | None = None,
) -> tuple[bool, str | None]:
    """Prove the restore actually took, rather than trusting a zero exit code.

    ``revision copy`` returning 0 means ARM accepted the request. Reporting
    "restored" on that alone is the same class of claim this whole gate exists
    to stop believing. What must be true is that the app is now serving a
    revision that is NOT the failed one and that runs the captured image.

    Returns (confirmed, revision now serving).
    """

    do_sleep = sleep or time.sleep
    current: str | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            app = az_json(
                ["containerapp", "show", "-g", resource_group, "-n", snapshot.name]
            )
        except AzError:
            app = None
        if isinstance(app, dict):
            current = traffic_revision(app)
            image = container_image(app)
            moved_off_the_failure = bool(current) and current != replaced_revision
            image_matches = snapshot.image is None or image == snapshot.image
            if moved_off_the_failure and image_matches:
                return True, current
        if attempt < attempts:
            do_sleep(delay)
    return False, current


def cmd_rollback(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    restored = 0
    failed = 0
    for service in SERVICES:
        snapshot = state.app(service)
        if snapshot is None:
            continue
        # Everything for one app is wrapped, so a timeout or a transient ARM
        # error on the first app cannot abandon the other two mid-rollback.
        try:
            try:
                app = az_json(
                    ["containerapp", "show", "-g", state.resourceGroup, "-n", snapshot.name]
                )
            except AzError as exc:
                emit("rollback", service=service, outcome="unreadable", detail=str(exc))
                annotate(
                    "error",
                    f"{service}: could not be read, so its revision was not restored.",
                )
                failed += 1
                continue
            current = traffic_revision(app) if isinstance(app, dict) else None
            commands = rollback_commands(
                resource_group=state.resourceGroup,
                snapshot=snapshot,
                current_revision=current,
            )
            if not commands:
                emit(
                    "rollback",
                    service=service,
                    outcome="skipped",
                    current=current,
                    captured=snapshot.revision,
                    detail="nothing to restore",
                )
                continue
            issued = True
            for command in commands:
                # `revision copy` waits for the new revision to provision, which
                # routinely exceeds the default read timeout.
                code, _, err = run_az([*command, "-o", "none"], timeout=args.az_timeout)
                if code != 0:
                    emit(
                        "rollback",
                        service=service,
                        outcome="failed",
                        captured=snapshot.revision,
                        detail=redact(err) or "az exited non-zero",
                    )
                    annotate(
                        "error",
                        f"{service}: could not restore revision {snapshot.revision}; "
                        "the app is still serving the failed deploy.",
                    )
                    failed += 1
                    issued = False
                    break
            if not issued:
                continue
            confirmed, serving = confirm_restored(
                resource_group=state.resourceGroup,
                snapshot=snapshot,
                replaced_revision=current,
                attempts=args.confirm_attempts,
                delay=args.confirm_delay,
            )
            if confirmed:
                restored += 1
                emit(
                    "rollback",
                    service=service,
                    outcome="restored",
                    captured=snapshot.revision,
                    replaced=current,
                    serving=serving,
                )
            else:
                failed += 1
                emit(
                    "rollback",
                    service=service,
                    outcome="unconfirmed",
                    captured=snapshot.revision,
                    replaced=current,
                    serving=serving,
                )
                annotate(
                    "error",
                    f"{service}: the restore was accepted but the app is not yet serving "
                    f"the captured image. Check `az containerapp revision list -g "
                    f"{state.resourceGroup} -n {snapshot.name}` before trusting this app.",
                )
        except AzError as exc:
            emit("rollback", service=service, outcome="failed", detail=str(exc))
            annotate("error", f"{service}: rollback failed ({exc}).")
            failed += 1
    emit("rollback_summary", restored=restored, failed=failed)
    return 4 if failed else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove an AI4IA deploy actually landed, and roll it back when it did not."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    capture = sub.add_parser("capture", help="Record the pre-deploy active revisions.")
    capture.add_argument("--state", required=True, help="Path to write the capture JSON.")
    capture.add_argument(
        "--environment",
        default=os.environ.get("AZURE_ENV_NAME", ""),
        help="azd environment name (default: AZURE_ENV_NAME).",
    )
    capture.add_argument(
        "--workload",
        default=os.environ.get("AI4IA_WORKLOAD", ""),
        help=f"Workload token used in the resource group name (default: {DEFAULT_WORKLOAD}).",
    )
    capture.add_argument(
        "--resource-group",
        default="",
        help="Override the derived resource group name.",
    )
    capture.set_defaults(func=cmd_capture)

    verify = sub.add_parser("verify", help="Assert the deploy landed and the app serves.")
    verify.add_argument("--state", required=True, help="Path to the capture JSON.")
    verify.add_argument("--api-url", default="", help="Override the API base URL.")
    verify.add_argument("--web-url", default="", help="Override the web base URL.")
    verify.add_argument("--proxy-url", default="", help="Override the proxy base URL.")
    verify.add_argument(
        "--proxy-path",
        default="/startup",
        help="Proxy probe path (SimpleL7Proxy serves /startup, /readiness, /health).",
    )
    verify.add_argument("--attempts", type=int, default=10, help="Attempts per HTTP probe.")
    verify.add_argument(
        "--proxy-attempts",
        type=int,
        default=15,
        help="Attempts for the proxy probe, which may be a scale-to-zero cold start.",
    )
    verify.add_argument(
        "--rollout-attempts",
        type=int,
        default=20,
        help="Attempts to observe a healthy new revision (ARM lags azd deploy).",
    )
    verify.add_argument(
        "--rollout-delay", type=float, default=10.0, help="Seconds between rollout reads."
    )
    verify.add_argument("--delay", type=float, default=5.0, help="Seconds between attempts.")
    verify.add_argument("--http-timeout", type=float, default=25.0, help="Per-request timeout.")
    verify.add_argument(
        "--deadline-seconds",
        type=float,
        default=1200.0,
        help=(
            "Total wall-clock budget for all checks. Per-check attempt budgets multiply, "
            "so this is what keeps the step inside its timeout. 0 disables."
        ),
    )
    verify.add_argument("--skip-canary", action="store_true", help="Do not run the model canary.")
    verify.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"Env var holding the canary bearer token (default: {DEFAULT_TOKEN_ENV}).",
    )
    verify.add_argument("--canary-model", default="", help="Pin the canary model id.")
    verify.add_argument("--canary-attempts", type=int, default=3, help="Canary retry budget.")
    verify.add_argument(
        "--canary-timeout", type=float, default=90.0, help="Canary per-request timeout."
    )
    verify.add_argument(
        "--models",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "infra" / "models.json",
        help="Path to infra/models.json.",
    )
    verify.set_defaults(func=cmd_verify)

    rollback = sub.add_parser("rollback", help="Restore the captured revisions.")
    rollback.add_argument("--state", required=True, help="Path to the capture JSON.")
    rollback.add_argument(
        "--az-timeout",
        type=float,
        default=900.0,
        help="Seconds to allow a restore command; `revision copy` waits for provisioning.",
    )
    rollback.add_argument(
        "--confirm-attempts",
        type=int,
        default=12,
        help="Reads used to confirm the restore actually took.",
    )
    rollback.add_argument(
        "--confirm-delay", type=float, default=10.0, help="Seconds between confirmation reads."
    )
    rollback.set_defaults(func=cmd_rollback)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("api_url", "web_url", "proxy_url", "canary_model", "resource_group"):
        if getattr(args, name, None) == "":
            setattr(args, name, None)
    try:
        return int(args.func(args))
    except VerifyInputError as exc:
        annotate("error", str(exc))
        emit("configuration_error", detail=str(exc))
        return 2
    except AzError as exc:
        annotate("error", f"Azure CLI call failed: {exc}")
        emit("az_error", detail=str(exc))
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
