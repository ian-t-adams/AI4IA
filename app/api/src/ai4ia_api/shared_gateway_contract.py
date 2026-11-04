"""Strict, secretless validation for the resolved shared gateway v2 manifest."""
from __future__ import annotations

from dataclasses import dataclass
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

    gateway = _object(top["gateway"], {"baseUrl", "allowedHosts", "pathPrefix"}, "gateway")
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
