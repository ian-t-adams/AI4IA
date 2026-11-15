"""Construct offline C1/C2/C3 request bodies; never read Azure or apply a request."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

API_VERSION = "2025-01-01"
C3_BLOCKER = (
    "C3 requires the exact offline-classified manifest and its non-activation sidecar"
)
C3_CLASSIFIED_MANIFESTS = {
    "94c2edf581b40a2677f3f90fad76eb2fc957478e5969c5007132d35040e72cb5":
        "nonconfidential-operational",
}
C3_SIDECAR_SHA256 = "f73a55378dbe418e92ca4ea3d0bc8e30ef6ae09778f3aebddc3525e5b571ecc4"
C3_QUALIFIED_LOADER_SHA256 = "cd89beaffe81acb0dd344ddd8a81d1a0c78e890e"
C3_MANIFEST_SECRET = "shared-gateway-activation-manifest"
C3_SELECTOR = "AI4IA_SHARED_GATEWAY_ENABLED"
C3_DEPLOYMENTS = "AI4IA_SHARED_GATEWAY_DEPLOYMENTS"
C3_MANIFEST = "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST"
# Literal R2 env entries passed to the API's own Settings validators; anything
# else is never read by validate_shared_gateway/validate_shared_gateway_catalog.
C3_RUNTIME_FIELDS = {
    "AI4IA_ENV": "env",
    "AI4IA_AUTH_PROVIDER": "auth_provider",
    "AI4IA_CHAT_PILOT_ISOLATION_ENABLED": "chat_pilot_isolation_enabled",
    C3_SELECTOR: "shared_gateway_enabled",
    "AI4IA_SHARED_GATEWAY_CLAUDE_MESSAGES_ENABLED": "shared_gateway_claude_messages_enabled",
    "AI4IA_SHARED_GATEWAY_VIDEO_JOBS_ENABLED": "shared_gateway_video_jobs_enabled",
    C3_MANIFEST: "shared_gateway_activation_manifest",
    C3_DEPLOYMENTS: "shared_gateway_deployments",
    "AZURE_CLIENT_ID": "AZURE_CLIENT_ID",
    "AI4IA_DATA_RESIDENCY": "data_residency",
    "AI4IA_CLAUDE_ENABLED": "claude_enabled",
    "AI4IA_CLAUDE_EXTERNAL_ENABLED": "claude_external_enabled",
    "AI4IA_CLAUDE_GATEWAY_ENABLED": "claude_gateway_enabled",
}
# The container's catalog path cannot be checked against local bytes.
C3_FORBIDDEN_ENV = frozenset({"AI4IA_MODEL_CATALOG_PATH"})
C3_DEPLOYMENT_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?"
ROOT = Path(__file__).resolve().parents[1]


def validate_revision_rows(
    evidence: object, app_name: str, r0: str, suffix: str, *, now: datetime | None = None,
) -> None:
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"observedAt", "rows"}
    ):
        raise ValueError("revision input must contain only an observation time and reader rows")
    observed_at = evidence["observedAt"]
    if not isinstance(observed_at, str):
        raise TypeError("revision observation time must be a string")
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError as exc:
        raise ValueError("revision observation time is invalid") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("revision observation time must include a timezone offset")
    age_seconds = ((now or datetime.now(UTC)) - observed.astimezone(UTC)).total_seconds()
    if age_seconds < -30 or age_seconds > 120:
        raise ValueError("revision rows must be observed within the last 120 seconds")
    rows = evidence["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 639:
        raise ValueError("revision rows must match the private revision reader's bounded shape")
    names: list[str] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "active", "health", "state"}
            or type(row["active"]) is not bool
            or row["health"] is not None and not isinstance(row["health"], str)
            or row["state"] is not None and not isinstance(row["state"], str)
            or not isinstance(row["name"], str)
            or len(row["name"]) > 64
            or re.fullmatch(re.escape(app_name) + r"--[a-z0-9-]{1,80}", row["name"]) is None
        ):
            raise ValueError("existing revision rows are incomplete or ambiguous")
        names.append(row["name"])
    if len(set(names)) != len(names) or r0 not in names or not any(
        row["name"] == r0 and row["active"] is True for row in rows
    ):
        raise ValueError("existing revision rows are incomplete or ambiguous")
    if app_name + "--" + suffix in names:
        raise ValueError("candidate revision name already exists")


def writable_template_projection(template: dict[str, Any]) -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts._private_recovery_packets import activation_template_projection

    return activation_template_projection(template)


TEMPLATE_DIFF_KEYS = frozenset({
    "containers", "initContainers", "volumes", "scale", "serviceBinds",
    "terminationGracePeriodSeconds", "name", "image", "imageType", "command", "args",
    "env", "resources", "cpu", "memory", "probes", "volumeMounts", "minReplicas",
    "maxReplicas", "cooldownPeriod", "pollingInterval", "rules", "http", "tcp",
    "azureQueue", "custom", "auth", "type", "initialDelaySeconds", "periodSeconds",
    "timeoutSeconds", "failureThreshold", "successThreshold", "httpGet", "tcpSocket",
    "port", "path", "scheme", "httpHeaders", "host", "volumeName", "mountPath",
    "subPath", "storageType", "storageName", "secrets", "mountOptions", "value",
    "secretRef", "serviceId", "clientType",
})
SCALE_DEFAULTS = {"cooldownPeriod": 300, "pollingInterval": 30}


def canonical_template(value: Any) -> Any:
    """Comparison only: retain list positions, dropping recursively empty dict entries."""
    if isinstance(value, dict):
        children = {key: canonical_template(child) for key, child in value.items()}
        return {key: child for key, child in children.items() if child not in (None, [], {})}
    if isinstance(value, list):
        return [canonical_template(child) for child in value]
    return value


def compare_r0_templates(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left = writable_template_projection(left)
    right = writable_template_projection(right)
    entries: list[dict[str, Any]] = []
    truncated = False
    compatible_kinds = True
    normalized_fields: set[str] = set()
    absent = object()

    def type_name(value: Any) -> str:
        if value is absent:
            return "absent"
        return {type(None): "null", bool: "bool", int: "int", float: "float",
                str: "str", list: "list", dict: "dict"}[type(value)]

    def record(path: list[str | int], kind: str, a: Any, b: Any) -> None:
        nonlocal truncated, compatible_kinds
        compatible_kinds = compatible_kinds and kind in (
            "absent_vs_empty", "null_vs_empty", "provider_default", "numeric_repr",
        )
        if len(entries) < 32:
            entry: dict[str, Any] = {"path": path, "kind": kind}
            if kind in ("type", "missing_left", "missing_right", "absent_vs_empty",
                        "provider_default", "numeric_repr"):
                entry.update(left_type=type_name(a), right_type=type_name(b))
            entries.append(entry)
        else:
            truncated = True

    def walk(a: Any, b: Any, path: list[str | int]) -> None:
        nonlocal truncated
        default = SCALE_DEFAULTS.get(path[1]) if len(path) == 2 and path[0] == "scale" else None
        if default is not None and (
                ((a is None or a is absent) and type(b) is int and b == default)
                or ((b is None or b is absent) and type(a) is int and a == default)):
            normalized_fields.add(path[1])
            record(path, "provider_default", a, b)
        elif default is not None and (
                (type(a) is int and type(b) is float and b.is_integer() and a == b)
                or (type(b) is int and type(a) is float and a.is_integer() and a == b)):
            normalized_fields.add(path[1])
            record(path, "numeric_repr", a, b)
        elif a is absent:
            record(path, "absent_vs_empty" if canonical_template(b) in (None, [], {})
                   else "missing_left", a, b)
        elif b is absent:
            record(path, "absent_vs_empty" if canonical_template(a) in (None, [], {})
                   else "missing_right", a, b)
        elif (a is None and canonical_template(b) in ([], {})
                or b is None and canonical_template(a) in ([], {})):
            record(path, "null_vs_empty", a, b)
        elif type(a) is not type(b):
            record(path, "type", a, b)
        elif isinstance(a, (dict, list)):
            if len(path) >= 16 and (a or b):
                record(path, "value", a, b)
                truncated = True
            elif isinstance(a, dict):
                for key in sorted(a.keys() | b.keys()):
                    child_path = [*path, key if key in TEMPLATE_DIFF_KEYS else "?"]
                    walk(a.get(key, absent), b.get(key, absent), child_path)
            else:
                if len(a) != len(b):
                    record(path, "length", a, b)
                for index, (aa, bb) in enumerate(zip(a, b, strict=False)):
                    walk(aa, bb, [*path, index])
        elif a != b:
            record(path, "value", a, b)

    walk(left, right, [])
    canonical_equal = canonical_template(left) == canonical_template(right)

    def comparison_form(template: dict[str, Any]) -> dict[str, Any]:
        value = canonical_template(template)
        scale = value.get("scale")
        if isinstance(scale, dict):
            # Only discard pairs already qualified at their exact schema paths.
            for key in normalized_fields:
                scale.pop(key, None)
        return canonical_template(value)

    return {
        "template_equal": left == right,
        "template_equal_canonical": canonical_equal,
        "compatible": compatible_kinds and comparison_form(left) == comparison_form(right),
        "template_diff": {"entries": entries, "truncated": truncated},
    }


STALE_C3_ENV = frozenset({C3_SELECTOR, C3_DEPLOYMENTS, C3_MANIFEST})


def stale_candidate_name(app_name: str, name: object) -> bool:
    """Only the release lane's own C2/C3 candidate names (candidate_suffix form)."""
    return isinstance(name, str) and re.fullmatch(
        re.escape(app_name) + r"--c[23]-[0-9a-f]{7}-[0-9]{1,8}", name) is not None


def stale_template_diff_ok(
    r0_template: dict[str, Any], stale_template: dict[str, Any], stale_name: str,
) -> bool:
    """A stale lane candidate may differ from R0 only in what its own stage changes.

    ``--c2-`` names: containers[0].image. ``--c3-`` names: the three C3 env entries.
    Everything else must be compatible under compare_r0_templates.
    """
    left = writable_template_projection(r0_template)
    right = writable_template_projection(stale_template)
    left_containers, right_containers = left.get("containers"), right.get("containers")
    if (not isinstance(left_containers, list) or not isinstance(right_containers, list)
            or len(left_containers) != 1 or len(right_containers) != 1
            or not isinstance(left_containers[0], dict) or not isinstance(right_containers[0], dict)):
        return False
    stage = re.search(r"--(c[23])-[0-9a-f]{7}-[0-9]{1,8}$", stale_name)
    if stage is None:
        return False
    if stage[1] == "c2":
        if "image" not in left_containers[0] or "image" not in right_containers[0]:
            return False
        right_containers[0]["image"] = left_containers[0]["image"]
    else:
        for container in (left_containers[0], right_containers[0]):
            env = container.get("env")
            if env is None:
                continue
            if not isinstance(env, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    for item in env):
                return False
            container["env"] = [
                item for item in env if item["name"].upper() not in STALE_C3_ENV]
    return bool(compare_r0_templates(left, right)["compatible"])


def git_blob_sha1(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def load_c3_contract(
    manifest_bytes: bytes, sidecar_bytes: bytes, api_identity_client_id: str,
) -> tuple[str, Any]:
    """Validate the one offline-classified manifest before parsing or embedding it."""
    raw_manifest, manifest, _ = _load_c3_contract(
        manifest_bytes, sidecar_bytes, api_identity_client_id,
    )
    return raw_manifest, manifest


def _load_c3_contract(
    manifest_bytes: bytes, sidecar_bytes: bytes, api_identity_client_id: str,
) -> tuple[str, Any, ModuleType]:
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if C3_CLASSIFIED_MANIFESTS.get(manifest_digest) != "nonconfidential-operational":
        raise ValueError("C3 manifest does not match the exact offline classification")
    if hashlib.sha256(sidecar_bytes).hexdigest() != C3_SIDECAR_SHA256:
        raise ValueError("C3 evidence sidecar digest does not match the pinned handoff")

    loader_path = ROOT / "app" / "api" / "src" / "ai4ia_api" / "shared_gateway_contract.py"
    loader_bytes = loader_path.read_bytes()
    if git_blob_sha1(loader_bytes) != C3_QUALIFIED_LOADER_SHA256:
        raise ValueError("C3 requires the qualified shared gateway contract loader")

    try:
        raw_manifest = manifest_bytes.decode("utf-8")
        sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("C3 manifest or evidence sidecar JSON is invalid") from exc
    if (
        not isinstance(sidecar, dict)
        or type(sidecar.get("activationReady")) is not bool
        or sidecar["activationReady"] is not False
        or type(sidecar.get("applicationChanged")) is not bool
        or sidecar["applicationChanged"] is not False
        or type(sidecar.get("modelRequests")) is not int
        or sidecar["modelRequests"] != 0
    ):
        raise ValueError("C3 evidence sidecar does not prove the required non-activation state")

    module_name = f"_ai4ia_qualified_gateway_contract_{uuid4().hex}"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(loader_bytes, "<qualified-shared-gateway-contract>", "exec"), module.__dict__)  # noqa: S102 - digest-pinned repository source
        manifest = module.load_shared_gateway_manifest(
            raw_manifest,
            api_identity_client_id=api_identity_client_id,
            require_api_identity=True,
            pilot_mode=False,
        )
    except ValueError as exc:
        raise ValueError(
            "C3 manifest is rejected by the qualified shared gateway contract loader"
        ) from exc
    finally:
        del sys.modules[module_name]
    if (
        manifest.contract_version != "2.2.0"
        or manifest.status != "resolved"
        or any(capability.enabled for capability in manifest.capabilities.values())
        or sidecar.get("callerClientId") != manifest.caller_client_id
        or sidecar.get("baseUrl") != manifest.base_url
    ):
        raise ValueError("C3 manifest or sidecar binding is not qualified for offline construction")
    return raw_manifest, manifest, module


def load_c3_deployments(deployments_bytes: bytes, expected_sha256: str) -> str:
    """Return the separately reviewed opt-in list only when its exact bytes match."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None:
        raise ValueError("C3 deployment opt-in digest must be 64 lowercase hex characters")
    if hashlib.sha256(deployments_bytes).hexdigest() != expected_sha256:
        raise ValueError("C3 deployment opt-in bytes do not match the reviewed digest")
    try:
        value = deployments_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("C3 deployment opt-in list must be ASCII") from exc
    if (
        len(value) > 4096
        or re.fullmatch(rf"{C3_DEPLOYMENT_NAME}(?:,{C3_DEPLOYMENT_NAME})*", value) is None
    ):
        raise ValueError(
            "C3 deployment opt-in list must be comma-separated deployment names "
            "without whitespace or a trailing newline"
        )
    names = value.split(",")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("C3 deployment opt-in list is ambiguous")
    return value


def validate_c3_runtime(env: list[dict[str, Any]], contract_module: ModuleType) -> None:
    """Run the API's own startup gateway validators against the literal R2 env.

    The digest-checked loader module is bound as ``ai4ia_api.shared_gateway_contract``
    for the duration, so the validators never import a cached or re-read loader.
    """
    kwargs: dict[str, str] = {}
    for item in env:
        key = item["name"].upper()
        if key in C3_FORBIDDEN_ENV:
            raise ValueError(f"C3 cannot validate a revision that sets {key}")
        if key not in C3_RUNTIME_FIELDS:
            continue
        if (
            item["name"] != key or set(item) != {"name", "value"}
            or not isinstance(item["value"], str)
        ):
            raise ValueError(f"C3 runtime validation requires {key} as an exact literal")
        kwargs[C3_RUNTIME_FIELDS[key]] = item["value"]

    source = str(ROOT / "app" / "api" / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    contract_name = "ai4ia_api.shared_gateway_contract"
    missing = object()
    previous = sys.modules.get(contract_name, missing)
    sys.modules[contract_name] = contract_module
    try:
        from ai4ia_api import catalog as runtime_catalog
        from ai4ia_api.config import Settings

        class C3Settings(Settings):
            @classmethod
            def settings_customise_sources(cls, settings_cls, init_settings, **_sources):
                return (init_settings,)

        runtime_catalog.load_catalog.cache_clear()
        try:
            settings = C3Settings(**kwargs)
            settings.validate_shared_gateway()
            settings.validate_shared_gateway_catalog()
        except (RuntimeError, ValueError, OSError) as exc:
            raise ValueError(
                "C3 revision is rejected by the API shared gateway startup validators"
            ) from exc
        finally:
            runtime_catalog.load_catalog.cache_clear()
    finally:
        if previous is missing:
            sys.modules.pop(contract_name, None)
        else:
            sys.modules[contract_name] = previous


def build_payload(
    app: dict[str, Any],
    revision: dict[str, Any],
    *,
    resource_id: str,
    stage: str,
    service: str = "api",
    suffix: str = "",
    api_image: str = "",
    web_image: str = "",
    existing_revisions: dict[str, Any] | None = None,
    manifest_bytes: bytes | None = None,
    classification_sidecar_bytes: bytes | None = None,
    deployments_bytes: bytes | None = None,
    deployments_sha256: str | None = None,
    stale_latest: str | None = None,
    stale_template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require a quiescent R0 snapshot, then pin it in the same request.

    ``stale_latest`` (C2/C3 only) names an inactive, untrafficked lane candidate left
    as latestRevisionName by an earlier unpromoted run; ``stale_template`` is its
    revision template. The default None keeps the strict latest == R0 rule.
    """
    if stage not in ("C1", "C2", "C3"):
        raise ValueError("stage must be C1, C2 or C3")
    if (stale_latest is None) != (stale_template is None):
        raise ValueError("stale latest requires both its name and its revision template")
    if stale_latest is not None and stage not in ("C2", "C3"):
        raise ValueError("stale latest is accepted only for C2/C3")
    if (
        service not in ("api", "web")
        or service == "web" and stage == "C3"
        or service == "web" and stage not in ("C1", "C2")
    ):
        raise ValueError("web service supports only C1 and C2 staging")
    if stage != "C3" and (
        manifest_bytes is not None or classification_sidecar_bytes is not None
        or deployments_bytes is not None or deployments_sha256 is not None
    ):
        raise ValueError("manifest inputs are accepted only for C3")
    if (
        re.fullmatch(
            r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+"
            r"/providers/Microsoft\.App/containerApps/[a-z0-9-]+",
            resource_id,
        ) is None
        or not isinstance(app.get("id"), str)
        or app["id"].casefold() != resource_id.casefold()
        or resource_id.rsplit("/", 1)[-1] != app.get("name")
    ):
        raise ValueError("snapshot must match the explicit Container App resource ID")
    if service == "web" and re.fullmatch(
        r"ca-web-[a-z0-9]+(?:-[a-z0-9]+)*", app["name"],
    ) is None:
        raise ValueError("web resource name must identify its exact environment")
    props = app["properties"]
    config = props["configuration"]
    rev = revision["properties"]
    r0 = revision["name"]
    if stale_latest is None:
        def latest_ok() -> bool:
            return (props.get("latestRevisionName") == r0
                    and props.get("latestReadyRevisionName") == r0)

        def app_template_ok() -> bool:
            return compare_r0_templates(props["template"], rev["template"])["compatible"]
    else:
        def latest_ok() -> bool:
            return True

        def app_template_ok() -> bool:
            return compare_r0_templates(props["template"], stale_template)["compatible"]
    # ARM may return provider/segment casing that differs from the explicit ID.
    if (
        not isinstance(revision.get("id"), str)
        or revision["id"].casefold() != (resource_id + "/revisions/" + r0).casefold()
        or props.get("provisioningState") != "Succeeded"
        or not latest_ok()
        or rev.get("active") is not True
        or rev.get("healthState") != "Healthy"
        or rev.get("runningState") not in ("Running", "RunningAtMaxScale")
        or not app_template_ok()
    ):
        raise ValueError("R0 must be healthy, active, latest/ready, with an identical writable template")
    if stale_latest is not None and (
        not stale_candidate_name(app["name"], stale_latest)
        or stale_latest == r0
        or props.get("latestRevisionName") != stale_latest
        or props.get("latestReadyRevisionName") not in (r0, stale_latest)
        or not stale_template_diff_ok(rev["template"], stale_template, stale_latest)
    ):
        raise ValueError("stale latest must be this lane's inactive stage-only revision")
    mode = "Single" if stage == "C1" else "Multiple"
    if config.get("activeRevisionsMode") != mode:
        raise ValueError(f"{stage} requires {mode} mode")
    traffic = config["ingress"]["traffic"]
    if len(traffic) != 1 or traffic[0].get("weight") != 100:
        raise ValueError("require a single 100% serving rule without labels")
    rule = traffic[0]
    named = rule == {"revisionName": r0, "weight": 100}
    latest = rule == {"latestRevision": True, "weight": 100}
    if not named and not (stage == "C1" and latest):
        raise ValueError("traffic must identify R0; C3 forbids latestRevision and labels")
    containers = rev["template"]["containers"]
    if len(containers) != 1 or containers[0].get("name") != service:
        raise ValueError(f"{stage} requires exactly one {service} container")
    if re.fullmatch(r".+@sha256:[0-9a-f]{64}", containers[0]["image"]) is None:
        raise ValueError("R0 image must be digest-pinned")
    payload: dict[str, Any] = {
        "location": app["location"],
        "properties": {
            "configuration": {
                "activeRevisionsMode": "Multiple",
                "ingress": {"traffic": [{"revisionName": r0, "weight": 100}]},
            },
        },
    }
    if stage == "C1":
        if (
            suffix or api_image or web_image or existing_revisions is not None
            or manifest_bytes is not None or classification_sidecar_bytes is not None
        ):
            raise ValueError("C1 accepts no candidate inputs")
        return payload
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", suffix) is None:
        raise ValueError("candidate suffix must be a lowercase revision suffix")
    if len(app["name"] + "--" + suffix) > 64 or r0 == app["name"] + "--" + suffix:
        raise ValueError("candidate revision name must be new and at most 64 characters")
    template = writable_template_projection(rev["template"])
    env = template["containers"][0].get("env", [])
    by_name = {item["name"]: item for item in env}
    if len(by_name) != len(env):
        raise ValueError("duplicate environment variable")

    def require_disabled(name: str) -> None:
        item = by_name.get(name)
        if item is not None and (set(item) != {"name", "value"} or item["value"] != "false"):
            raise ValueError(f"{name} must be absent or a literal false value")

    template["revisionSuffix"] = suffix
    if stage == "C3":
        if (
            api_image or web_image or existing_revisions is None
            or manifest_bytes is None or classification_sidecar_bytes is None
            or deployments_bytes is None or deployments_sha256 is None
        ):
            raise ValueError(
                "C3 requires only its exact manifest, sidecar, reviewed deployment list, "
                "suffix and revision rows"
            )
        validate_revision_rows(existing_revisions, app["name"], r0, suffix)
        env = template["containers"][0].get("env", [])
        if not isinstance(env, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in env
        ):
            raise ValueError("C3 environment is malformed")
        names = [item["name"] for item in env]
        if len({name.upper() for name in names}) != len(names):
            raise ValueError("duplicate environment variable")
        by_name = {item["name"]: item for item in env}
        identity = by_name.get("AZURE_CLIENT_ID")
        if (
            identity is None or set(identity) != {"name", "value"}
            or not isinstance(identity["value"], str)
        ):
            raise ValueError("C3 requires one literal API managed identity client ID")
        # R1-OFF is C2's unchanged output: selector absent or literal false, and no
        # opt-in list or manifest reference yet.
        require_disabled(C3_SELECTOR)
        pilot = by_name.get("AI4IA_CHAT_PILOT_ISOLATION_ENABLED")
        if pilot is not None and (
            set(pilot) != {"name", "value"} or pilot["value"] != "false"
        ):
            raise ValueError("C3 requires chat pilot isolation to remain disabled")
        if any(
            name.upper() in (C3_DEPLOYMENTS, C3_MANIFEST) for name in names
        ) or any(item.get("secretRef") == C3_MANIFEST_SECRET for item in env):
            raise ValueError(
                "C3 requires the R1-OFF baseline without a deployment opt-in list "
                "or manifest reference"
            )
        deployments = load_c3_deployments(deployments_bytes, deployments_sha256)
        raw_manifest, _, contract_module = _load_c3_contract(
            manifest_bytes, classification_sidecar_bytes, identity["value"],
        )
        selector = {"name": C3_SELECTOR, "value": "true"}
        if C3_SELECTOR in by_name:
            env[names.index(C3_SELECTOR)] = selector
        else:
            env.append(selector)
        env.append({"name": C3_DEPLOYMENTS, "value": deployments})
        env.append({"name": C3_MANIFEST, "value": raw_manifest})
        validate_c3_runtime(env, contract_module)
        template["containers"][0]["env"] = env
        payload["properties"]["template"] = template
        return payload
    if stage == "C2":
        candidate_image = api_image if service == "api" else web_image
        other_image = web_image if service == "api" else api_image
        if (
            other_image or not candidate_image or existing_revisions is None
        ):
            raise ValueError(
                f"C2 requires a {service} image and revision rows from the private revision reader only"
            )
        if service == "api":
            require_disabled("AI4IA_CHAT_PILOT_ISOLATION_ENABLED")
            require_disabled("AI4IA_SHARED_GATEWAY_ENABLED")
        validate_revision_rows(existing_revisions, app["name"], r0, suffix)
        acr_image = re.compile(
            rf"(?P<registry>[a-z0-9]+\.azurecr\.io)/ai4ia/"
            rf"(?P<repository>{service}-[a-z0-9]+(?:-[a-z0-9]+)*)"
            r"@sha256:[0-9a-f]{64}"
        )
        old_image = containers[0]["image"]
        old_match = acr_image.fullmatch(old_image)
        new_match = acr_image.fullmatch(candidate_image)
        expected_repository = old_match["repository"] if old_match is not None else ""
        if service == "web":
            app_name = re.fullmatch(
                r"ca-web-(?P<environment>[a-z0-9]+(?:-[a-z0-9]+)*)",
                app["name"],
            )
            if app_name is None:
                raise ValueError("web resource name must identify its exact environment")
            expected_repository = "web-" + app_name["environment"]
        if (
            old_match is None
            or new_match is None
            or old_match["repository"] != expected_repository
            or old_match["registry"] != new_match["registry"]
            or old_match["repository"] != new_match["repository"]
            or old_image == candidate_image
        ):
            raise ValueError(
                f"candidate must be a new digest-pinned image in R0's exact {service} repository"
            )
        template["containers"][0]["image"] = candidate_image
        payload["properties"]["template"] = template
        return payload
    raise ValueError("unsupported stage")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--revision", type=Path, required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--stage", choices=("C1", "C2", "C3"), required=True)
    parser.add_argument("--service", choices=("api", "web"), default="api")
    parser.add_argument("--suffix", default="")
    parser.add_argument("--api-image", default="")
    parser.add_argument("--web-image", default="")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--classification-sidecar", type=Path)
    parser.add_argument(
        "--deployments", type=Path,
        help="C3 only: exact reviewed AI4IA_SHARED_GATEWAY_DEPLOYMENTS value bytes",
    )
    parser.add_argument(
        "--deployments-sha256",
        help="C3 only: separately reviewed SHA-256 of the --deployments bytes",
    )
    parser.add_argument(
        "--existing-revisions", type=Path,
        help="JSON rows from private_revision_rows; this offline helper cannot prove pagination completeness",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.stage == "C3" and (
            args.manifest is None or args.classification_sidecar is None
        ):
            raise ValueError(C3_BLOCKER)
        if args.stage == "C3" and (
            args.deployments is None or args.deployments_sha256 is None
        ):
            raise ValueError("C3 requires the reviewed deployment list and its digest")
        if args.stage != "C3" and (
            args.manifest is not None or args.classification_sidecar is not None
            or args.deployments is not None or args.deployments_sha256 is not None
        ):
            raise ValueError("manifest inputs are accepted only for C3")
        private_paths = [
            path for path in (
                args.app, args.revision, args.existing_revisions,
                args.manifest, args.classification_sidecar, args.deployments, args.output,
            )
            if path is not None
        ]
        if any(path.resolve().is_relative_to(ROOT.resolve()) for path in private_paths):
            raise ValueError(
                "snapshot, manifest, and payload files must stay outside the repository"
            )
        payload = build_payload(
            json.loads(args.app.read_text(encoding="utf-8-sig")),
            json.loads(args.revision.read_text(encoding="utf-8-sig")),
            resource_id=args.resource_id,
            stage=args.stage,
            service=args.service,
            suffix=args.suffix,
            api_image=args.api_image,
            web_image=args.web_image,
            existing_revisions=(
                json.loads(args.existing_revisions.read_text(encoding="utf-8-sig"))
                if args.existing_revisions else None
            ),
            manifest_bytes=args.manifest.read_bytes() if args.manifest else None,
            classification_sidecar_bytes=(
                args.classification_sidecar.read_bytes()
                if args.classification_sidecar else None
            ),
            deployments_bytes=args.deployments.read_bytes() if args.deployments else None,
            deployments_sha256=args.deployments_sha256,
        )
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Cutover payload refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
