"""Strict, secretless validation for the resolved shared gateway v2 manifest."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from urllib.parse import urlparse


_PATHS = frozenset({"/chat/completions", "/responses"})
_MODEL_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HOST = re.compile(r"^[a-z0-9.-]+$")
_BASE_URL = re.compile(r"^https://[a-z0-9.-]+(?::[0-9]+)?/openai/v1$")
_AUDIENCE = re.compile(r"^api://[A-Za-z0-9._-]+$")
_MODEL_SKUS = frozenset({
    "GlobalStandard", "DataZoneStandard", "Standard", "ProvisionedManaged",
    "GlobalProvisionedManaged", "DataZoneProvisionedManaged",
})
_MAX_MANIFEST_BYTES = 65536
_V21_REQUIRED_FORBIDDEN_HEADERS = frozenset({
    "api-key", "x-llmmodel", "s7p-model-override", "x-backend-affinity",
    "x-s7ppriority", "x-policycyclecounter", "s7pdebug",
    "x-ms-client-principal-id", "x-userid",
})


class SharedGatewayManifestError(ValueError):
    """The configured manifest is incomplete, unresolved, or unsupported."""


@dataclass(frozen=True)
class SharedGatewayManifest:
    base_url: str
    caller_client_id: str
    scope: str
    operations: dict[str, bool]
    model_operations: dict[str, frozenset[str]]
    model_kinds: dict[str, str]
    model_deployments: dict[str, str]
    model_skus: dict[str, frozenset[str]]
    terminal_usage_verified: bool
    forbidden_headers: frozenset[str]
    contract_version: str = "2.0.0"
    status: str = "resolved"
    capabilities: dict[str, SharedGatewayCapability] = field(default_factory=dict)


@dataclass(frozen=True)
class SharedGatewayCapability:
    enabled: bool
    base_url: str | None
    models: dict[str, dict]


def _object(value: object, required: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise SharedGatewayManifestError(f"Shared gateway manifest {label} shape is invalid.")
    return value


def _guid(value: object) -> bool:
    return isinstance(value, str) and _GUID.fullmatch(value) is not None


def load_shared_gateway_manifest(
    raw: str | None, *, api_identity_client_id: str | None,
    require_api_identity: bool = True,
) -> SharedGatewayManifest:
    if not isinstance(raw, str) or not raw:
        raise SharedGatewayManifestError("A resolved shared gateway contract v2 manifest is required.")
    try:
        manifest_bytes = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SharedGatewayManifestError("Shared gateway manifest encoding is invalid.") from exc
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise SharedGatewayManifestError("Shared gateway manifest exceeds 65536 UTF-8 bytes.")
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SharedGatewayManifestError("Shared gateway manifest JSON is invalid.") from exc

    if isinstance(manifest, dict) and manifest.get("contractVersion") == "2.1.0":
        manifest = json.loads(raw, object_pairs_hook=_unique_keys)
        return _load_v21(
            manifest, api_identity_client_id=api_identity_client_id,
            require_api_identity=require_api_identity,
        )

    top = _object(
        manifest,
        {
            "contractVersion", "status", "gateway", "auth", "operations", "models",
            "retry", "responses", "streaming", "correlation", "privacy", "bypass",
        },
        "top-level",
    )
    if top["contractVersion"] != "2.0.0" or top["status"] != "resolved":
        raise SharedGatewayManifestError("Only a resolved shared gateway contract v2 is enableable.")

    return _load_common(
        top, api_identity_client_id=api_identity_client_id,
        require_api_identity=require_api_identity,
    )


def _load_common(
    top: dict, *, api_identity_client_id: str | None, require_api_identity: bool,
    extra_gateway_fields: set[str] | None = None,
) -> SharedGatewayManifest:
    gateway = _object(
        top["gateway"],
        {"baseUrl", "allowedHosts", "pathPrefix"} | (extra_gateway_fields or set()),
        "gateway",
    )
    base_url = gateway["baseUrl"]
    if (
        not isinstance(base_url, str)
        or _BASE_URL.fullmatch(base_url) is None
        or gateway["pathPrefix"] != "/openai/v1"
        or not isinstance(gateway["allowedHosts"], list)
        or not gateway["allowedHosts"]
        or any(
            not isinstance(host, str) or _HOST.fullmatch(host) is None
            for host in gateway["allowedHosts"]
        )
        or len(set(gateway["allowedHosts"])) != len(gateway["allowedHosts"])
    ):
        raise SharedGatewayManifestError("Shared gateway endpoint or host allowlist is invalid.")
    parsed_base = urlparse(base_url)
    try:
        port = parsed_base.port
    except ValueError as exc:
        raise SharedGatewayManifestError("Shared gateway endpoint port is invalid.") from exc
    if (
        (parsed_base.hostname or "") not in gateway["allowedHosts"]
        or port is not None and not 1 <= port <= 65535
    ):
        raise SharedGatewayManifestError("Shared gateway endpoint is not in its exact host allowlist.")

    auth = _object(
        top["auth"],
        {
            "tenantId", "issuer", "audience", "scope", "accessTokenVersion",
            "callerClientId", "credential",
        },
        "auth",
    )
    tenant_id = auth["tenantId"]
    caller_client_id = auth["callerClientId"]
    audience = auth["audience"]
    if (
        not _guid(tenant_id)
        or not _guid(caller_client_id)
        or auth["issuer"] != f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        or not isinstance(audience, str)
        or _AUDIENCE.fullmatch(audience) is None
        or auth["scope"] != f"{audience}/.default"
        or type(auth["accessTokenVersion"]) is not int
        or auth["accessTokenVersion"] != 2
        or auth["credential"] != "managed-identity"
        or (
            require_api_identity
            and (
                not isinstance(api_identity_client_id, str)
                or not _guid(api_identity_client_id.lower())
            )
        )
        or (
            api_identity_client_id is not None
            and (
                not isinstance(api_identity_client_id, str)
                or caller_client_id != api_identity_client_id.lower()
            )
        )
    ):
        raise SharedGatewayManifestError(
            "Shared gateway token issuer, audience, version, credential or API identity is invalid."
        )

    operations_value = top["operations"]
    if not isinstance(operations_value, list) or not operations_value:
        raise SharedGatewayManifestError("Shared gateway operation allowlist is empty.")
    operations: dict[str, bool] = {}
    for operation in operations_value:
        item = _object(operation, {"method", "path", "stream"}, "operation")
        if (
            item["method"] != "POST"
            or not isinstance(item["path"], str)
            or item["path"] not in _PATHS
            or not isinstance(item["stream"], str)
            or item["stream"] not in {"forbidden", "allowed"}
            or item["path"] in operations
        ):
            raise SharedGatewayManifestError("Shared gateway operation allowlist is unsupported.")
        operations[item["path"]] = item["stream"] == "allowed"

    models_value = top["models"]
    if not isinstance(models_value, dict) or not models_value:
        raise SharedGatewayManifestError("Shared gateway model allowlist is empty.")
    model_operations: dict[str, frozenset[str]] = {}
    model_kinds: dict[str, str] = {}
    model_deployments: dict[str, str] = {}
    model_skus: dict[str, frozenset[str]] = {}
    for model_key, model in models_value.items():
        if not isinstance(model_key, str) or _MODEL_KEY.fullmatch(model_key) is None:
            raise SharedGatewayManifestError("Shared gateway model keys must be exact lowercase names.")
        item = _object(model, {"kind", "operations", "targets"}, "model")
        model_paths = item["operations"]
        targets = item["targets"]
        if (
            not isinstance(item["kind"], str)
            or item["kind"] not in {"deployment-name", "alias"}
            or not isinstance(model_paths, list)
            or not model_paths
            or any(not isinstance(path, str) or path not in operations for path in model_paths)
            or len(set(model_paths)) != len(model_paths)
            or not isinstance(targets, list)
            or not targets
        ):
            raise SharedGatewayManifestError("Shared gateway model allowlist entry is invalid.")
        for target in targets:
            if (
                not isinstance(target, dict)
                or set(target) != {"deployment", "sku"}
                or not isinstance(target["deployment"], str)
                or not target["deployment"]
                or not isinstance(target["sku"], str)
                or target["sku"] not in _MODEL_SKUS
            ):
                raise SharedGatewayManifestError("Shared gateway model target is invalid.")
        target_names = {target["deployment"] for target in targets}
        if len(target_names) != 1:
            raise SharedGatewayManifestError(
                "Shared gateway v2 models cannot spill across deployment names."
            )
        if (
            item["kind"] == "deployment-name"
            and model_key != next(iter(target_names)).lower()
        ):
            raise SharedGatewayManifestError(
                "Shared gateway deployment-name keys must match their target."
            )
        model_operations[model_key] = frozenset(model_paths)
        model_kinds[model_key] = item["kind"]
        model_deployments[model_key] = next(iter(target_names))
        model_skus[model_key] = frozenset(target["sku"] for target in targets)

    retry = _object(
        top["retry"],
        {
            "owner", "clientSdkMaxRetries", "clientAppFailover",
            "replayAfterFirstByte", "retryAfterHonored",
        },
        "retry",
    )
    if (
        retry["owner"] != "gateway"
        or type(retry["clientSdkMaxRetries"]) is not int
        or retry["clientSdkMaxRetries"] != 0
        or retry["clientAppFailover"] is not False
        or retry["replayAfterFirstByte"] is not False
        or retry["retryAfterHonored"] is not True
    ):
        raise SharedGatewayManifestError("Shared gateway retry ownership is unsupported.")

    responses = _object(top["responses"], {"storeRequired", "forbiddenFields"}, "responses")
    forbidden_fields = {"previous_response_id", "background", "conversation"}
    if (
        responses["storeRequired"] is not False
        or not isinstance(responses["forbiddenFields"], list)
        or any(not isinstance(field, str) for field in responses["forbiddenFields"])
        or set(responses["forbiddenFields"]) != forbidden_fields
        or len(responses["forbiddenFields"]) != len(forbidden_fields)
    ):
        raise SharedGatewayManifestError("Shared gateway Responses contract is unsupported.")

    streaming = _object(
        top["streaming"], {"format", "cancellation", "terminalUsage"}, "streaming",
    )
    if (
        streaming["format"] != "text/event-stream"
        or streaming["cancellation"] != "client-disconnect"
        or not isinstance(streaming["terminalUsage"], str)
        or streaming["terminalUsage"] not in {"unverified", "verified"}
    ):
        raise SharedGatewayManifestError("Shared gateway streaming contract is unsupported.")

    correlation = _object(
        top["correlation"], {"responseHeader", "servedBackendGuaranteed"}, "correlation",
    )
    if (
        correlation["responseHeader"] != "x-S7P-ID"
        or correlation["servedBackendGuaranteed"] is not False
    ):
        raise SharedGatewayManifestError("Shared gateway correlation contract is unsupported.")

    privacy = _object(
        top["privacy"],
        {"reviewCapture", "userAttribution", "forbiddenRequestHeaders"},
        "privacy",
    )
    forbidden_headers = privacy["forbiddenRequestHeaders"]
    if (
        privacy["reviewCapture"] != "off"
        or privacy["userAttribution"] != "disabled"
        or not isinstance(forbidden_headers, list)
        or any(not isinstance(name, str) or not name for name in forbidden_headers)
        or len(set(name.lower() for name in forbidden_headers)) != len(forbidden_headers)
        or {"authorization", "content-type"} & {name.lower() for name in forbidden_headers}
    ):
        raise SharedGatewayManifestError("Shared gateway privacy contract is unsupported.")

    bypass = _object(
        top["bypass"],
        {"automaticDirectFallback", "operatorBypass", "captureDenyPreservedOnBypass"},
        "bypass",
    )
    if (
        bypass["automaticDirectFallback"] is not False
        or bypass["operatorBypass"] != "clear-base-url-with-logged-marker"
        or bypass["captureDenyPreservedOnBypass"] is not True
    ):
        raise SharedGatewayManifestError("Shared gateway bypass contract is unsupported.")

    return SharedGatewayManifest(
        base_url=base_url,
        caller_client_id=caller_client_id,
        scope=auth["scope"],
        operations=operations,
        model_operations=model_operations,
        model_kinds=model_kinds,
        model_deployments=model_deployments,
        model_skus=model_skus,
        terminal_usage_verified=streaming["terminalUsage"] == "verified",
        forbidden_headers=frozenset(name.lower() for name in forbidden_headers),
    )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise SharedGatewayManifestError("Shared gateway 2.1 duplicate JSON key.")
        result[key] = value
    return result


def _load_v21(
    manifest: object, *, api_identity_client_id: str | None, require_api_identity: bool,
) -> SharedGatewayManifest:
    top = _object(
        manifest,
        {
            "contractVersion", "status", "gateway", "auth", "operations", "models",
            "retry", "responses", "streaming", "correlation", "privacy", "bypass",
            "capabilities",
        },
        "2.1 top-level",
    )
    if top["status"] not in ("resolved", "schema-only"):
        raise SharedGatewayManifestError("Shared gateway 2.1 status is invalid.")
    gateway = top["gateway"]
    if not isinstance(gateway, dict):
        raise SharedGatewayManifestError("Shared gateway 2.1 gateway shape is invalid.")
    extras = set(gateway) & {"anthropicBaseUrl", "videoBaseUrl"}
    common = _load_common(
        top, api_identity_client_id=api_identity_client_id,
        require_api_identity=require_api_identity, extra_gateway_fields=extras,
    )
    if not _V21_REQUIRED_FORBIDDEN_HEADERS <= common.forbidden_headers:
        raise SharedGatewayManifestError("Shared gateway 2.1 required forbidden headers are missing.")
    base = urlparse(common.base_url)
    for name in extras:
        path = "/anthropic/v1" if name == "anthropicBaseUrl" else "/video/v1"
        value = gateway[name]
        if (
            not isinstance(value, str)
            or re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?" + path, value) is None
            or urlparse(value).netloc != base.netloc
        ):
            raise SharedGatewayManifestError("Shared gateway 2.1 family URL is invalid.")

    capabilities = _object(
        top["capabilities"], {"claudeMessages", "videoJobs"}, "2.1 capabilities",
    )
    templates = {
        "claudeMessages": {
            "messages": {"method": "POST", "path": "/messages", "stream": "allowed"},
        },
        "videoJobs": {
            "submit": {"method": "POST", "path": "/jobs"},
            "status": {"method": "GET", "path": "/jobs/{id}"},
            "content": {"method": "GET", "path": "/jobs/{id}/content"},
        },
    }
    parsed: dict[str, SharedGatewayCapability] = {}
    for name, template in templates.items():
        family = _object(capabilities[name], {"enabled", "operations", "models"}, name)
        models = family["models"]
        if (
            type(family["enabled"]) is not bool
            or family["operations"] != template
            or not isinstance(models, dict)
        ):
            raise SharedGatewayManifestError("Shared gateway 2.1 capability is invalid.")
        paths = {operation["path"] for operation in template.values()}
        for key, model in models.items():
            if not isinstance(key, str) or _MODEL_KEY.fullmatch(key) is None:
                raise SharedGatewayManifestError("Shared gateway 2.1 model key is invalid.")
            item = _object(model, {"kind", "operations", "targets"}, "2.1 model")
            targets = item["targets"]
            operations = item["operations"]
            if (
                item["kind"] not in ("deployment-name", "alias")
                or not isinstance(operations, list)
                or any(not isinstance(path, str) for path in operations)
                or len(operations) != len(paths)
                or set(operations) != paths
                or not isinstance(targets, list)
                or not targets
            ):
                raise SharedGatewayManifestError("Shared gateway 2.1 model is invalid.")
            deployments: set[str] = set()
            seen: list[dict] = []
            for target in targets:
                target = _object(target, {"deployment", "sku", "backend"}, "2.1 target")
                deployment, sku, backend = (
                    target["deployment"], target["sku"], target["backend"]
                )
                if (
                    not isinstance(deployment, str) or not deployment
                    or any(ord(char) < 32 or ord(char) == 127 for char in deployment)
                    or not isinstance(sku, str) or sku not in _MODEL_SKUS
                    or not isinstance(backend, str)
                    or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", backend) is None
                    or target in seen
                ):
                    raise SharedGatewayManifestError("Shared gateway 2.1 target is invalid.")
                deployments.add(deployment)
                seen.append(target)
            if len(deployments) != 1 or (
                (key == next(iter(deployments)).lower()) != (item["kind"] == "deployment-name")
            ):
                raise SharedGatewayManifestError("Shared gateway 2.1 model binding is invalid.")
        url_field = "anthropicBaseUrl" if name == "claudeMessages" else "videoBaseUrl"
        if family["enabled"] and (
            top["status"] != "resolved" or url_field not in gateway or not models
        ):
            raise SharedGatewayManifestError("Shared gateway 2.1 enabled family is incomplete.")
        parsed[name] = SharedGatewayCapability(
            enabled=family["enabled"], base_url=gateway.get(url_field), models=models,
        )

    hosts = gateway["allowedHosts"]
    if top["status"] == "schema-only":
        if (
            any(family.enabled for family in parsed.values())
            or any(
                host != "example.invalid" and not host.endswith(".example.invalid") for host in hosts
            )
            or top["auth"]["tenantId"] != "00000000-0000-0000-0000-000000000000"
            or common.caller_client_id != "00000000-0000-0000-0000-000000000000"
            or ".example.invalid" not in top["auth"]["audience"]
        ):
            raise SharedGatewayManifestError("Shared gateway schema-only contract must be synthetic and off.")
    elif (
        any(host == "example.invalid" or host.endswith(".example.invalid") for host in hosts)
        or top["auth"]["tenantId"] == "00000000-0000-0000-0000-000000000000"
        or common.caller_client_id == "00000000-0000-0000-0000-000000000000"
        or "example.invalid" in top["auth"]["audience"]
    ):
        raise SharedGatewayManifestError("Shared gateway resolved contract contains placeholders.")
    return replace(
        common, contract_version="2.1.0", status=top["status"], capabilities=parsed,
    )
