"""Private recovery inputs: existing module contracts, never secret-value export."""
from __future__ import annotations

from copy import deepcopy
import json
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
    "cutover": "web.bicep",
}
PATCH_FORBIDDEN_KEYS = frozenset({"configuration", "identity", "secrets", "ingress", "tags", "location"})


class PacketError(ValueError):
    """Fixed messages only: inputs can contain private configuration."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PacketError(message)


def activation_patch_template(current: dict, legacy: dict) -> dict:
    """Copy the current template; change only the approved flag and production maximum."""
    template = deepcopy(current["properties"]["template"])
    containers = template.get("containers")
    require(isinstance(containers, list) and len(containers) == 1 and containers[0].get("name") == "api",
            "Activation requires the exact single API container.")
    resources = containers[0].get("resources")
    require(isinstance(resources, dict) and type(resources.get("cpu")) is float and resources["cpu"] == 0.5
            and resources.get("memory") == "1Gi" and containers[0].get("imageType") == "ContainerImage",
            "Activation compute/image type differs from the approved profile.")
    scale = template.get("scale")
    production = legacy["properties"]["template"].get("scale")
    require(isinstance(scale, dict) and isinstance(production, dict)
            and type(scale.get("minReplicas")) is int and scale["minReplicas"] == 1
            and type(scale.get("maxReplicas")) is int and scale["maxReplicas"] == 1
            and type(production.get("minReplicas")) is int and production["minReplicas"] == 1
            and type(production.get("maxReplicas")) is int and production["maxReplicas"] == 3,
            "Activation scale is not the reviewed current/legacy profile.")
    rows = containers[0].get("env")
    require(isinstance(rows, list) and len(rows) <= 512, "Activation environment is unavailable.")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) <= {"name", "value", "secretRef"}
                and isinstance(row.get("name"), str) and row["name"] not in seen,
                "Activation environment names or fields are invalid.")
        require(not ("value" in row and "secretRef" in row)
                and ("value" not in row or isinstance(row["value"], str))
                and ("secretRef" not in row or isinstance(row["secretRef"], str) and bool(row["secretRef"])),
                "Activation environment value/reference mode is invalid.")
        seen.add(row["name"])
    flags = {row["name"]: row for row in rows if row["name"] in {
        "AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED",
    }}
    require(flags == {
        name: {"name": name, "value": "true"} for name in
        ("AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED")
    }, "Activation source is not privately ready and worker-suppressed.")
    flags["AI4IA_PRIVATE_RECOVERY_CANDIDATE"]["value"] = "false"
    scale["maxReplicas"] = production["maxReplicas"]
    return activation_template_projection(template)


def activation_template_projection(template: dict) -> dict:
    """Only the revision claim and documented read-only storage are provider fields."""
    template = deepcopy(template)
    template.pop("revisionSuffix", None)
    for name in ("containers", "initContainers"):
        group = template.get(name)
        require(group is None or isinstance(group, list), "Activation container collection is invalid.")
        for container in group or []:
            require(isinstance(container, dict), "Activation container shape is invalid.")
            resources = container.get("resources")
            require(resources is None or isinstance(resources, dict), "Activation resources are invalid.")
            if resources is not None:
                require(resources.get("ephemeralStorage") is None or isinstance(resources["ephemeralStorage"], str),
                        "Activation read-only storage shape is unknown.")
                resources.pop("ephemeralStorage", None)
    return template


def activation_suffix(binding: str) -> str:
    require(re.fullmatch(r"[0-9]{10}:[0-9a-f]{64}", binding) is not None, "Activation binding is invalid.")
    return "activate-" + binding.split(":")[1][:16]


def activation_patch_body(template: dict, binding: str) -> dict:
    desired = deepcopy(template)
    require("revisionSuffix" not in desired, "Activation suffix must be derived from the binding.")
    desired["revisionSuffix"] = activation_suffix(binding)
    body = {"properties": {"template": desired}}
    pending: list[tuple[Any, int]] = [(body, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        require(count <= 16384 and depth <= 32, "Activation PATCH exceeds its shape bound.")
        if isinstance(value, dict):
            require(not PATCH_FORBIDDEN_KEYS.intersection(value), "Forbidden field in activation PATCH.")
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    require(len(json.dumps(body, allow_nan=False).encode()) <= 512 * 1024, "Activation PATCH exceeds its byte bound.")
    return body


def validate_activation_patch_body(body: Any, current: dict, legacy: dict, binding: str) -> None:
    expected = activation_patch_body(activation_patch_template(current, legacy), binding)
    require(isinstance(body, dict) and set(body) == {"properties"}
            and isinstance(body["properties"], dict) and set(body["properties"]) == {"template"},
            "Activation PATCH must contain only properties.template.")
    require(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
            == json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False),
            "Activation PATCH changed image, environment, probes or unapproved template fields.")


def location_evidence(data: Any, subscription: str, expected: str) -> dict[str, str]:
    """Bind one Region's name/displayName from the scoped ARM locations response."""
    require(isinstance(expected, str) and re.fullmatch(r"[a-z][a-z0-9]{1,63}", expected) is not None
            and expected != "global", "Approved canonical location is invalid.")
    require(isinstance(data, dict) and data.get("nextLink") is None
            and isinstance(data.get("rows"), list) and 0 < len(data["rows"]) <= 512,
            "Azure location identity coverage is incomplete.")
    rows = data["rows"]
    for row in rows:
        require(isinstance(row, dict)
                and all(isinstance(row.get(key), str) and 0 < len(row[key]) <= 128
                        for key in ("name", "displayName")),
                "Azure location identity metadata is malformed.")
    matches = [row for row in rows if row["name"] == expected]
    require(len(matches) == 1, "Approved Azure location identity is absent or ambiguous.")
    selected = matches[0]
    resource_id = f"/subscriptions/{subscription}/locations/{expected}"
    require(selected.get("type") == "Region" and isinstance(selected.get("id"), str)
            and selected["id"].casefold() == resource_id.casefold(),
            "Azure location identity does not match the approved subscription and Region.")
    aliases = {selected["name"].casefold(), selected["displayName"].casefold()}
    require(all(row is selected or not aliases.intersection(
        {row["name"].casefold(), row["displayName"].casefold()}) for row in rows),
        "Azure location representations are ambiguous.")
    return {key: selected[key] for key in ("id", "name", "displayName", "type")}


def canonical_location(value: Any, evidence: dict[str, str]) -> str:
    require(isinstance(value, str) and value.casefold() in {
        evidence["name"].casefold(), evidence["displayName"].casefold(),
    }, "Location is missing, unknown or different from the approved Region.")
    return evidence["name"]


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
    if ingress.get("transport") == "Auto":
        ingress["transport"] = "auto"
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
    if result.get("maxInactiveRevisions") is None:
        result.pop("maxInactiveRevisions", None)
    if result.get("identitySettings") == []:
        result.pop("identitySettings")
    for registry in result.get("registries", []):
        for key in ("username", "passwordSecretRef"):
            if registry.get(key) in (None, ""):
                registry.pop(key, None)
        if isinstance(registry.get("identity"), str):
            registry["identity"] = registry["identity"].casefold()
    return result


def candidate_template_intent(template: dict) -> dict:
    """Only observed null optional members; populated settings remain comparable."""
    result = deepcopy(template)
    for key in ("initContainers", "serviceBinds", "terminationGracePeriodSeconds", "volumes"):
        if result.get(key) is None:
            result.pop(key, None)
    scale = result.get("scale")
    if isinstance(scale, dict) and scale.get("rules") is None:
        scale.pop("rules", None)
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
