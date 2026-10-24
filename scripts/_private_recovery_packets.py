"""Private recovery inputs: existing module contracts, never secret-value export."""
from __future__ import annotations

from copy import deepcopy
import re
import math
from typing import Any, Callable

SECRET_NAMES = frozenset({
    "model-gateway-api-key", "webiq-api-key", "admin-api-secret",
    "realtime-gateway-api-key", "realtime-ga-gateway-api-key",
    "speech-voice-live-gateway-api-key", "code-interpreter-api-key",
    "official-mcp-subscription-key",
})
MODULES = {
    "foundation": "api-private-network.bicep",
    "candidate": "api.bicep",
    "activate": "api.bicep",
    "cutover": "web.bicep",
}


class PacketError(ValueError):
    """Fixed messages only: inputs can contain private configuration."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PacketError(message)


def secret_descriptors(rows: Any, identity: str) -> list[dict[str, str]]:
    require(isinstance(rows, list) and len(rows) <= len(SECRET_NAMES),
            "Secret metadata is missing or oversized.")
    result = []
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) <= {"name", "keyVaultUrl", "identity"},
                "Secret metadata contains forbidden fields.")
        name = row.get("name")
        require(isinstance(name, str) and name in SECRET_NAMES and name not in seen,
                "Secret metadata has an unknown or duplicate name.")
        seen.add(name)
        descriptor = {"name": name}
        url, assigned = row.get("keyVaultUrl"), row.get("identity")
        if url is not None:
            require(isinstance(url, str) and re.fullmatch(
                r"https://[a-zA-Z0-9-]{3,24}\.vault\.azure\.net/secrets/"
                r"[a-zA-Z0-9-]{1,127}(?:/[a-fA-F0-9]{32})?", url,
            ) is not None, "Key Vault reference metadata is invalid.")
            require(isinstance(assigned, str) and assigned.casefold() == identity.casefold(),
                    "Key Vault reference does not use the existing API identity.")
            descriptor.update(keyVaultUrl=url, identity=assigned)
        else:
            require(assigned is None, "Literal secret metadata has an unexpected identity.")
        result.append(descriptor)
    return sorted(result, key=lambda row: row["name"])


def record_parameters(
    template: dict, metadata: dict, values: dict, overrides: dict,
) -> dict:
    """A historical record supplies candidates, not proof of current configuration."""
    definitions = template["parameters"]
    names, kinds = metadata.get("names"), metadata.get("types")
    if not isinstance(names, list) or not isinstance(kinds, list):
        raise PacketError("Existing module parameter inventory is incompatible.")
    require(metadata.get("state") == "Succeeded"
            and isinstance(metadata.get("timestamp"), str)
            and isinstance(metadata.get("hash"), str),
            "Existing module deployment metadata is incomplete.")
    require(isinstance(names, list) and isinstance(kinds, list)
            and len(names) == len(kinds) and 0 < len(names) <= 256
            and all(isinstance(name, str) and name in definitions for name in names)
            and len(set(names)) == len(names),
            "Existing module parameter inventory is incompatible.")
    recorded = {name: kind for name, kind in zip(names, kinds, strict=True)}
    allowed = set()
    for name, kind in recorded.items():
        require(isinstance(kind, str) and kind.casefold() == definitions[name]["type"].casefold(),
                "Existing module parameter types changed.")
        if kind.casefold() not in ("securestring", "secureobject"):
            allowed.add(name)
    require(set(values) == allowed, "Non-secret parameter projection is incomplete.")
    require(set(overrides) <= set(definitions)
            and all(definitions[name]["type"].casefold() not in ("securestring", "secureobject")
                    for name in overrides),
            "Recovery cannot inject secret values or unknown module parameters.")
    for name, value in {**values, **overrides}.items():
        kind = definitions[name]["type"].casefold()
        valid = {
            "string": isinstance(value, str), "bool": type(value) is bool,
            "int": type(value) is int and -(2 ** 63) <= value < 2 ** 63,
            "array": isinstance(value, list), "object": isinstance(value, dict),
        }
        require(valid.get(kind, False), "Existing module parameter value has an invalid type.")
    params = {name: {"value": deepcopy(value)} for name, value in values.items()}
    params.update({name: {"value": deepcopy(value)} for name, value in overrides.items()})
    for name in set(definitions) - set(params):
        require("defaultValue" in definitions[name], "Required module input is unavailable.")
        if definitions[name]["type"].casefold() in ("securestring", "secureobject"):
            require(definitions[name]["defaultValue"] == "",
                    "Secret input has no empty default; secure reuse cannot proceed.")
    tags = params.get("tags", {}).get("value")
    if isinstance(tags, dict) and "SecurityControl" in tags:
        require(tags.pop("SecurityControl") == "Ignore", "Unreviewed security control tag.")
    return params


def read_record(
    read: Callable[[list[str]], Any], template: dict, subscription: str, group: str, name: str,
) -> tuple[dict, dict]:
    """CLI output projects secure parameter types, never their values."""
    require(name in ("api", "web"), "Only existing API/web module records may be read.")
    base = ["deployment", "group", "show", "--subscription", subscription, "-g", group, "-n", name]
    query = (
        "{timestamp:properties.timestamp,hash:properties.templateHash,"
        "state:properties.provisioningState,names:keys(properties.parameters),"
        "types:values(properties.parameters)[].type}"
    )
    metadata = read([*base, "--query", query])
    require(isinstance(metadata, dict), "Existing module record is unavailable.")
    names, kinds = metadata.get("names"), metadata.get("types")
    require(isinstance(names, list) and isinstance(kinds, list) and len(names) == len(kinds)
            and 0 < len(names) <= 256, "Existing module type inventory is unavailable.")
    definitions = template["parameters"]
    projection = []
    for field, kind in zip(names, kinds, strict=True):
        require(isinstance(field, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", field) is not None
                and field in definitions and isinstance(kind, str)
                and kind.casefold() == definitions[field]["type"].casefold(),
                "Existing module record does not match the current parameter contract.")
        if kind.casefold() not in ("securestring", "secureobject"):
            projection.append(f"{field}:{field}.value")
    require(len(set(names)) == len(names) and bool(projection), "Module parameter projection is invalid.")
    values = read([*base, "--query", "properties.parameters.{" + ",".join(projection) + "}"])
    require(isinstance(values, dict) and read([*base, "--query", query]) == metadata,
            "Existing module record changed during parameter resolution.")
    return metadata, values


def api_overrides(
    *, phase: str, subscription: str, group: str, workload: str, environment: str,
    source: dict, image: str,
) -> dict:
    require(phase in ("candidate", "activate"), "Unexpected API recovery step.")
    root = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/"
    identity = root + f"Microsoft.ManagedIdentity/userAssignedIdentities/id-api-{environment}"
    old_name = f"ca-api-{environment}" if phase == "candidate" else f"ca-api-private-{environment}"
    require(source.get("id", "").casefold() == (root + "Microsoft.App/containerApps/" + old_name).casefold(),
            "API source identity is not the selected same-group application.")
    assigned = source.get("identity", {})
    keys = assigned.get("userAssignedIdentities", {})
    require(assigned.get("type") == "UserAssigned" and isinstance(keys, dict)
            and all(isinstance(key, str) for key in keys)
            and {key.casefold() for key in keys} == {identity.casefold()},
            "API source must use only the existing API identity.")
    configuration = source["properties"]["configuration"]
    registries = configuration.get("registries")
    require(isinstance(registries, list) and len(registries) == 1
            and registries[0].get("identity", "").casefold() == identity.casefold(),
            "Existing API registry identity is unknown.")
    host = registries[0].get("server", "")
    require(isinstance(host, str) and re.fullmatch(r"[a-z0-9]+\.azurecr\.io", host) is not None
            and re.fullmatch(re.escape(host + f"/ai4ia/api-{environment}@sha256:") + r"[0-9a-f]{64}", image) is not None,
            "An exact existing-registry API image is required.")
    descriptors = secret_descriptors(configuration.get("secrets"), identity)
    require(isinstance(source.get("tags"), dict), "Current API tags are unavailable.")
    return {
        "tags": deepcopy(source["tags"]),
        "appName": f"ca-api-private-{environment}",
        "containerEnvId": root + f"Microsoft.App/managedEnvironments/cae-{workload}-{environment}-api-private",
        "apiIdentityResourceId": identity,
        "apiImage": image,
        "recoveryCandidate": phase == "candidate",
        "privateCosmosRequired": True,
        "recoverySource": "legacy" if phase == "candidate" else "private",
        "recoverySecretDescriptors": descriptors,
    }


def configuration_intent(configuration: dict) -> dict:
    """Project documented/default service fields; refuse unrepresented configuration."""
    result = deepcopy(configuration)
    result["secrets"] = [
        {key: value for key, value in row.items() if key != "value" and value is not None}
        for row in result.get("secrets", [])
    ]
    for row in configuration.get("secrets", []):
        value = row.get("value")
        require(value is None or (isinstance(value, str) and re.fullmatch(r"\*{3,128}", value) is not None),
                "Provider secret value is unprotected or its masking is unknown.")
    result["secrets"].sort(key=lambda row: row["name"])
    ingress = result.get("ingress")
    if not isinstance(ingress, dict):
        raise PacketError("Ingress configuration is unavailable.")
    ingress.pop("fqdn", None)
    # Preserve populated ACL/CORS/session settings rather than projecting them away.
    for key in ("additionalPortMappings", "clientCertificateMode", "corsPolicy", "customDomains",
                "ipSecurityRestrictions", "stickySessions", "targetPortHttpScheme"):
        if ingress.get(key) is None:
            ingress.pop(key, None)
    if type(ingress.get("exposedPort")) is int and ingress["exposedPort"] == 0:
        ingress.pop("exposedPort")
    traffic = ingress.get("traffic")
    if (isinstance(traffic, list) and len(traffic) == 1
            and set(traffic[0]) == {"latestRevision", "weight"}
            and traffic[0]["latestRevision"] is True
            and type(traffic[0]["weight"]) is int and traffic[0]["weight"] == 100):
        ingress.pop("traffic")
    for key in ("dapr", "runtime", "service"):
        if result.get(key) is None:
            result.pop(key, None)
    for registry in result.get("registries", []):
        for key in ("username", "passwordSecretRef"):
            if registry.get(key) in (None, ""):
                registry.pop(key, None)
        if isinstance(registry.get("identity"), str):
            registry["identity"] = registry["identity"].casefold()
    return result


def consumption_headroom(data: Any, additional_cores: float) -> dict:
    require(isinstance(data, dict) and data.get("nextLink") in (None, "")
            and isinstance(data.get("value"), list) and 0 < len(data["value"]) <= 32,
            "Private environment quota coverage is incomplete.")
    rows = [row for row in data["value"] if isinstance(row, dict)
            and row.get("name", {}).get("value") == "ManagedEnvironmentConsumptionCores"]
    require(len(rows) == 1, "Private environment Consumption quota is unavailable.")
    row = rows[0]
    used, limit = row.get("currentValue"), row.get("limit")
    if (isinstance(used, bool) or not isinstance(used, (int, float))
            or isinstance(limit, bool) or not isinstance(limit, (int, float))):
        raise PacketError("Private environment core headroom is insufficient or unknown.")
    require(math.isfinite(used) and math.isfinite(limit) and 0 <= used <= limit
            and ("usage" not in row or (type(row["usage"]) in (int, float) and row["usage"] == used))
            and limit - used >= additional_cores, "Private environment core headroom is insufficient or unknown.")
    return {"current": used, "limit": limit, "requiredAdditionalCores": additional_cores}
