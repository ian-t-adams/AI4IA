"""Bounded, offline evaluation of the compiled API's non-secret binding metadata."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any


class MetadataError(ValueError):
    """Fixed diagnostic text; never include source values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MetadataError(message)


@dataclass(frozen=True)
class SecretSource:
    app_id: str
    name: str


@dataclass(frozen=True)
class EnvValue:
    empty: bool


OPAQUE = object()
TOKEN = re.compile(r"\s*('(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?|[(),.\[\]])")
MASK = re.compile(r"\*{3,128}")


class Expression:
    def __init__(self, text: str):
        require(len(text) <= 256 * 1024, "Metadata expression is oversized.")
        self.tokens: list[str] = []
        position = 0
        while position < len(text):
            match = TOKEN.match(text, position)
            require(match is not None, "Unsupported metadata expression syntax.")
            assert match is not None
            self.tokens.append(match[1])
            position = match.end()
        require(len(self.tokens) <= 32768, "Metadata expression has too many tokens.")
        self.index = 0

    def take(self, expected: str | None = None) -> str:
        require(self.index < len(self.tokens), "Incomplete metadata expression.")
        token = self.tokens[self.index]
        require(expected is None or token == expected, "Unexpected metadata expression token.")
        self.index += 1
        return token

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def node(self, depth: int = 0) -> tuple:
        require(depth <= 64, "Metadata expression is too deep.")
        token = self.take()
        if token.startswith("'"):
            result = ("literal", token[1:-1].replace("''", "'"))
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", token):
            result = ("literal", json.loads(token))
        elif token.casefold() in ("true", "false", "null") and self.peek() != "(":
            result = ("literal", json.loads(token.casefold()))
        else:
            self.take("(")
            args = []
            while self.peek() != ")":
                args.append(self.node(depth + 1))
                if self.peek() != ",":
                    break
                self.take(",")
            self.take(")")
            result = ("call", token.casefold(), args)
        while self.peek() in (".", "["):
            operator = self.take()
            if operator == ".":
                key = ("literal", self.take())
            else:
                key = self.node(depth + 1)
                self.take("]")
            result = ("get", result, key)
        return result

    def parse(self) -> tuple:
        result = self.node()
        require(self.peek() is None, "Metadata expression has trailing tokens.")
        return result


class MetadataEvaluator:
    """No ARM operations: listSecrets produces named origins, never credentials."""

    def __init__(self, template: dict, parameters: dict, subscription: str, group: str, baseline: dict):
        self.template, self.parameters = template, parameters
        self.subscription, self.group, self.baseline = subscription, group, baseline
        self.cache: dict[tuple[str, str], Any] = {}
        self.active: set[tuple[str, str]] = set()
        self.remaining = 50000

    def value(self, value: Any, mode: str = "plain", scope: dict | None = None) -> Any:
        if isinstance(value, str) and value.startswith("[["):
            return value[1:]
        if isinstance(value, str) and value.startswith("["):
            require(value.endswith("]"), "Incomplete ARM metadata expression.")
            return self.evaluate(Expression(value[1:-1]).parse(), mode, scope or {})
        if isinstance(value, dict):
            return {key: self.field(key, child, mode, scope or {}) for key, child in value.items()}
        if isinstance(value, list):
            require(len(value) <= 512, "Metadata array is oversized.")
            return [self.value(child, mode, scope) for child in value]
        return value

    def field(self, key: str, value: Any, mode: str, scope: dict, *, node: bool = False) -> Any:
        resolved = self.evaluate(value, "plain" if key == "value" else mode, scope) if node else self.value(
            value, "plain" if key == "value" else mode, scope,
        )
        if mode == "env" and key == "value":
            require(not isinstance(resolved, SecretSource), "A secret was moved into a value environment row.")
            return EnvValue(type(resolved) is str and resolved == "")
        return resolved

    def evaluate(self, node: tuple, mode: str, scope: dict) -> Any:
        self.remaining -= 1
        require(self.remaining >= 0, "Metadata evaluation exceeded its bound.")
        kind = node[0]
        if kind == "literal":
            return node[1]
        if kind == "get":
            obj, key = self.evaluate(node[1], mode, scope), self.evaluate(node[2], mode, scope)
            if obj is OPAQUE:
                return OPAQUE
            require(isinstance(obj, (dict, list)), "Metadata member access has an unknown source.")
            if isinstance(obj, dict):
                require(key in obj, "Metadata member is missing.")
            else:
                require(type(key) is int and 0 <= key < len(obj), "Metadata index is invalid.")
            return obj[key]
        name, args = node[1], node[2]
        arity = {
            "parameters": 1, "variables": 1, "lambdavariables": 1, "listsecrets": 2,
            "not": 1, "equals": 2, "empty": 1, "contains": 2, "tryget": 2,
            "first": 1, "string": 1, "json": 1,
        }
        require(name not in arity or len(args) == arity[name], "Invalid metadata function arity.")
        if name == "if":
            require(len(args) == 3, "Invalid metadata conditional.")
            condition = self.evaluate(args[0], mode, scope)
            require(type(condition) is bool, "Metadata condition is unknown.")
            return self.evaluate(args[1 if condition else 2], mode, scope)
        if name == "lambda":
            require(len(args) == 2, "Unsupported metadata lambda.")
            return (self.evaluate(args[0], mode, scope), args[1], dict(scope))
        if name in ("map", "filter"):
            require(len(args) == 2, "Invalid metadata collection operation.")
            rows = self.evaluate(args[0], mode, scope)
            variable, body, outer = self.evaluate(args[1], mode, scope)
            require(isinstance(rows, list) and len(rows) <= 512 and isinstance(variable, str),
                    "Metadata collection is invalid.")
            result = []
            for row in rows:
                value = self.evaluate(body, mode, {**outer, variable: row})
                if name == "map":
                    result.append(value)
                else:
                    require(type(value) is bool, "Metadata filter is unknown.")
                    if value:
                        result.append(row)
            return result
        if name == "createobject":
            require(len(args) % 2 == 0, "Invalid metadata object.")
            result = {}
            for index in range(0, len(args), 2):
                key = self.evaluate(args[index], mode, scope)
                require(isinstance(key, str) and key not in result, "Duplicate or invalid metadata object key.")
                result[key] = self.field(key, args[index + 1], mode, scope, node=True)
            return result
        values = [self.evaluate(arg, mode, scope) for arg in args]
        if name == "parameters":
            key = values[0]
            require(key in self.template["parameters"], "Unknown metadata parameter.")
            definition = self.template["parameters"][key]
            if definition["type"].casefold().startswith("secure"):
                require(key not in self.parameters and definition.get("defaultValue") == "",
                        "Secret parameter values cannot enter metadata evaluation.")
                return ""
            require(key in self.parameters or "defaultValue" in definition, "Missing metadata parameter.")
            return self.parameters[key]["value"] if key in self.parameters else self.value(definition["defaultValue"], mode, scope)
        if name == "variables":
            key = (values[0], mode)
            require(key not in self.active and len(self.active) < 64, "Cyclic metadata variables.")
            if key not in self.cache:
                require(key[0] in self.template.get("variables", {}), "Unknown metadata variable.")
                self.active.add(key)
                self.cache[key] = self.value(self.template["variables"][key[0]], mode, scope)
                self.active.remove(key)
            return self.cache[key]
        if name == "lambdavariables":
            require(values[0] in scope, "Unknown metadata lambda variable.")
            return scope[values[0]]
        if name == "listsecrets":
            require(len(values) == 2 and values[1] == "2025-07-01", "Unreviewed secret source API.")
            resource = self.template["resources"].get(values[0])
            require(isinstance(resource, dict) and resource.get("existing") is True
                    and resource.get("type") == "Microsoft.App/containerApps"
                    and resource.get("apiVersion") == values[1] and "scope" not in resource and "condition" not in resource,
                    "Secret source is not the bound existing application.")
            source_name = self.value(resource["name"], "plain", scope)
            source_id = f"/subscriptions/{self.subscription}/resourceGroups/{self.group}/providers/Microsoft.App/containerApps/{source_name}"
            require(source_id.casefold() == self.baseline["id"].casefold(), "Secret source application changed.")
            return {"value": [{"name": row["name"], "value": SecretSource(source_id, row["name"])}
                              for row in self.baseline["properties"]["configuration"]["secrets"]]}
        if name == "reference":
            return OPAQUE
        require(all(value is not OPAQUE and not isinstance(value, SecretSource) for value in values),
                "A metadata function depends on unavailable secret/provider data.")
        if name == "createarray":
            return values
        if name == "concat":
            if all(isinstance(value, list) for value in values):
                result = [item for value in values for item in value]
                require(len(result) <= 512, "Metadata concat is oversized.")
                return result
            require(all(isinstance(value, str) for value in values), "Unsupported metadata concat.")
            return "".join(values)
        if name in ("and", "or"):
            require(all(type(value) is bool for value in values), "Unknown metadata Boolean.")
            return all(values) if name == "and" else any(values)
        if name == "not":
            require(len(values) == 1 and type(values[0]) is bool, "Unknown metadata Boolean.")
            return not values[0]
        if name == "equals":
            return type(values[0]) is type(values[1]) and values[0] == values[1]
        if name == "empty":
            require(len(values) == 1 and isinstance(values[0], (str, list, dict)), "Unknown metadata emptiness.")
            return len(values[0]) == 0
        if name == "contains":
            require(isinstance(values[0], (str, list, dict)), "Unknown metadata membership.")
            return values[1] in values[0]
        if name == "tryget":
            require(isinstance(values[0], dict) and isinstance(values[1], str), "Invalid metadata optional access.")
            return values[0].get(values[1])
        if name == "coalesce":
            return next((value for value in values if value is not None), None)
        if name == "first":
            require(isinstance(values[0], list) and len(values[0]) == 1, "Secret lookup is absent or ambiguous.")
            return values[0][0]
        if name == "format":
            require(bool(values) and isinstance(values[0], str)
                    and all(token.isdigit() for token in re.findall(r"{([^}]+)}", values[0])),
                    "Unsupported metadata format.")
            return values[0].format(*values[1:])
        if name == "resourceid":
            require(len(values) >= 2 and all(isinstance(value, str) for value in values), "Invalid metadata resource ID.")
            parts = values[0].split("/")
            require(len(parts) == len(values), "Unreviewed resource ID scope.")
            suffix = "/".join(item for pair in zip(parts[1:], values[1:]) for item in pair)
            return f"/subscriptions/{self.subscription}/resourceGroups/{self.group}/providers/{parts[0]}/{suffix}"
        if name == "string":
            value = values[0]
            return value if isinstance(value, str) else str(value) if type(value) is bool else json.dumps(value, separators=(",", ":"))
        if name == "json":
            require(isinstance(values[0], str) and len(values[0]) <= 1024 * 1024, "Invalid metadata JSON input.")
            return json.loads(values[0])
        if name == "union":
            require(all(isinstance(value, dict) for value in values), "Unsupported metadata union.")
            return {key: child for value in values for key, child in value.items()}
        raise MetadataError("Unsupported metadata expression function.")


def secret_metadata(rows: Any) -> list[dict]:
    require(isinstance(rows, list) and len(rows) <= 8, "Secret metadata is missing or oversized.")
    result, names = [], set()
    for row in rows:
        require(isinstance(row, dict) and set(row) <= {"name", "value", "keyVaultUrl", "identity"},
                "Unexpected secret metadata fields.")
        name = row.get("name")
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9-]{1,64}", name) is not None and name not in names,
                "Secret names are unknown or duplicated.")
        names.add(name)
        url, identity = row.get("keyVaultUrl"), row.get("identity")
        if url is not None:
            require(isinstance(url, str) and isinstance(identity, str) and bool(url) and bool(identity)
                    and row.get("value") is None, "Key Vault metadata is incomplete.")
            assert isinstance(identity, str)
            result.append({"name": name, "kind": "keyVault", "keyVaultUrl": url, "identity": identity.casefold()})
        else:
            value = row.get("value")
            require(identity is None and (value is None or isinstance(value, SecretSource)
                    or isinstance(value, str) and MASK.fullmatch(value) is not None),
                    "Secret values cannot be read or substituted.")
            result.append({"name": name, "kind": "containerApp"})
    return sorted(result, key=lambda row: row["name"])


def env_metadata(rows: Any, *, source: bool = False) -> tuple[dict, set, set]:
    require(isinstance(rows, list) and len(rows) <= 512, "Environment metadata is incomplete.")
    refs, values, empty, seen = {}, set(), set(), set()
    for row in rows:
        require(isinstance(row, dict) and set(row) <= {"name", "value", "secretRef"}, "Environment binding has unknown fields.")
        name = row.get("name")
        require(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) is not None and name not in seen,
                "Environment binding name is invalid or duplicated.")
        seen.add(name)
        require(not ("value" in row and "secretRef" in row), "Environment value and secret reference are mixed.")
        if "secretRef" in row:
            require(isinstance(row["secretRef"], str) and bool(row["secretRef"]), "Empty environment secret reference.")
            refs[name] = row["secretRef"]
        else:
            values.add(name)
            value = row.get("value")
            if source:
                require(isinstance(value, EnvValue), "Source environment value is not proven.")
                assert isinstance(value, EnvValue)
                if value.empty:
                    empty.add(name)
            elif value == "" and type(value) is str:
                empty.add(name)
    return refs, values, empty


def source_descriptor(template: dict, parameters: dict, binding: dict, baseline: dict) -> dict:
    resources = template.get("resources")
    require(isinstance(resources, dict) and isinstance(resources.get("apiApp"), dict), "Compiled API resource is unavailable.")
    assert isinstance(resources, dict)
    app = resources["apiApp"]
    require(app.get("type") == "Microsoft.App/containerApps" and app.get("apiVersion") == "2024-10-02-preview"
            and "condition" not in app and "scope" not in app, "Unreviewed compiled API identity.")
    evaluator = MetadataEvaluator(template, parameters, binding["subscription"], binding["resourceGroup"], baseline)
    require(evaluator.value(app["name"]) == f"ca-api-private-{binding['environment']}", "Source API target changed.")
    configuration = app["properties"]["configuration"]
    secrets = evaluator.value(configuration["secrets"])
    stored = secret_metadata(secrets)
    require(stored == secret_metadata(baseline["properties"]["configuration"]["secrets"]),
            "Source secret metadata differs from the live baseline.")
    origins = []
    for row in secrets:
        if "value" in row:
            value = row["value"]
            require(isinstance(value, SecretSource) and value.app_id.casefold() == baseline["id"].casefold()
                    and value.name == row["name"], "Source secret value origin changed.")
            origins.append({"name": row["name"], "appId": value.app_id.casefold(), "secretName": value.name})
    containers = app["properties"]["template"]["containers"]
    require(isinstance(containers, list) and len(containers) == 1, "Compiled container inventory changed.")
    rows = evaluator.value(containers[0]["env"], "env")
    refs, values, empty = env_metadata(rows, source=True)
    old_containers = baseline["properties"]["template"]["containers"]
    require(len(old_containers) == 1, "Baseline container inventory changed.")
    old_refs, old_values, _ = env_metadata(old_containers[0]["env"])
    additions = {"AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED"}
    require(refs == old_refs and values - additions == old_values - additions, "Source environment binding surface changed.")
    require(set(refs.values()) <= {row["name"] for row in stored}, "Source environment references an undeclared secret.")
    return {"secrets": stored, "valueOrigins": sorted(origins, key=lambda row: row["name"]),
            "envSecretRefs": dict(sorted(refs.items())), "envValueNames": sorted(values), "emptyEnvValues": sorted(empty)}


def _masked_or_equal(actual: Any, expected: str, *, identity: bool = False) -> bool:
    return isinstance(actual, str) and (
        MASK.fullmatch(actual) is not None or (actual.casefold() == expected.casefold() if identity else actual == expected)
    )


def validate_native_metadata(app: dict, descriptor: dict) -> None:
    """Match an unordered mask projection to source, never assign names to masks."""
    secrets = app["properties"]["configuration"].get("secrets")
    expected = descriptor["secrets"]
    require(isinstance(secrets, list) and len(secrets) == len(expected) <= 8,
            "Native secret count differs from source.")
    known = set()
    candidates = []
    for row in secrets:
        require(isinstance(row, dict) and set(row) <= {"name", "value", "keyVaultUrl", "identity"},
                "Native secret structure is unknown.")
        name = row.get("name")
        require(isinstance(name, str), "Native secret name is absent.")
        assert isinstance(name, str)
        if MASK.fullmatch(name) is None:
            require(name not in known, "Native secret names are duplicated.")
            known.add(name)
        matches = []
        for index, secret in enumerate(expected):
            if not _masked_or_equal(name, secret["name"]):
                continue
            if secret["kind"] == "containerApp":
                value = row.get("value")
                if (row.get("keyVaultUrl") is None and row.get("identity") is None
                        and (value is None or isinstance(value, str) and MASK.fullmatch(value) is not None)):
                    matches.append(index)
            elif (row.get("value") is None and _masked_or_equal(row.get("keyVaultUrl"), secret["keyVaultUrl"])
                    and _masked_or_equal(row.get("identity"), secret["identity"], identity=True)):
                matches.append(index)
        require(bool(matches), "Native secret metadata differs from source.")
        candidates.append(matches)
    matched: dict[int, int] = {}

    def assign(row: int, visited: set[int]) -> bool:
        for target in candidates[row]:
            if target in visited:
                continue
            visited.add(target)
            if target not in matched or assign(matched[target], visited):
                matched[target] = row
                return True
        return False

    require(all(assign(index, set()) for index in range(len(secrets))), "Native secret source kinds differ.")
    containers = app["properties"]["template"].get("containers")
    require(isinstance(containers, list) and len(containers) == 1, "Native container inventory changed.")
    rows = containers[0].get("env")
    refs, values, _ = env_metadata(rows)
    require(set(refs) == set(descriptor["envSecretRefs"]) and values == set(descriptor["envValueNames"]),
            "Native environment binding modes differ from source.")
    for name, reference in refs.items():
        require(_masked_or_equal(reference, descriptor["envSecretRefs"][name]),
                "Native environment reference differs from source.")
    for row in rows:
        if row["name"] in values:
            require("value" in row or row["name"] in descriptor["emptyEnvValues"],
                    "Native environment value is unexpectedly absent.")


def project_proven_metadata(app: dict, descriptor: dict) -> dict:
    """Remove separately proven metadata, not replace masked fields with guesses."""
    result = deepcopy(app)
    result["properties"]["configuration"]["secrets"] = []
    for row in result["properties"]["template"]["containers"][0]["env"]:
        name = row["name"]
        if name in descriptor["envSecretRefs"]:
            require("secretRef" in row and "value" not in row, "Proven environment reference mode changed.")
            del row["secretRef"]
        elif name in descriptor["emptyEnvValues"]:
            require("value" not in row or type(row["value"]) is str and row["value"] == "",
                    "Source-empty environment value changed.")
            row.pop("value", None)
    return result


RAW_METADATA_QUERY = (
    "{id:id,revision:properties.latestRevisionName,"
    "secrets:properties.configuration.secrets[].{name:name,keyVaultUrl:keyVaultUrl,identity:identity},"
    "flags:properties.template.containers[0].env[?name=='AI4IA_PRIVATE_RECOVERY_CANDIDATE' || "
    "name=='AI4IA_PRIVATE_COSMOS_REQUIRED'].{name:name,value:value},"
    "containers:properties.template.containers[].{name:name,env:env[].{name:name,secretRef:secretRef,"
    "hasRef:contains(keys(@), 'secretRef'),hasValue:contains(keys(@), 'value'),emptyValue:value==''}}}"
)


def validate_live_metadata(snapshot: dict, descriptor: dict, expected_id: str, *, baseline: bool = False) -> dict:
    require(isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str)
            and snapshot["id"].casefold() == expected_id.casefold(), "Live secret metadata identity changed.")
    require(secret_metadata(snapshot.get("secrets")) == descriptor["secrets"], "Live secret metadata differs from source.")
    containers = snapshot.get("containers")
    require(isinstance(containers, list) and len(containers) == 1 and isinstance(containers[0], dict)
            and containers[0].get("name") == "api",
            "Live metadata container inventory changed.")
    assert isinstance(containers, list)
    rows = containers[0].get("env")
    require(isinstance(rows, list) and len(rows) <= 512, "Live environment metadata is incomplete.")
    assert isinstance(rows, list)
    refs, values, seen = {}, set(), set()
    for row in rows:
        require(isinstance(row, dict), "Live environment metadata row is invalid.")
        name = row.get("name")
        require(isinstance(name, str) and name not in seen and type(row.get("hasRef")) is bool
                and type(row.get("hasValue")) is bool, "Live environment metadata shape is invalid.")
        seen.add(name)
        if row["hasRef"]:
            require(not row["hasValue"] and isinstance(row.get("secretRef"), str), "Live value/reference mode changed.")
            refs[name] = row["secretRef"]
        else:
            values.add(name)
            require(row["hasValue"] or name in descriptor["emptyEnvValues"], "Live environment value is absent.")
            if name in descriptor["emptyEnvValues"]:
                require(not row["hasValue"] or row.get("emptyValue") is True, "Live source-empty value changed.")
    expected_values = set(descriptor["envValueNames"])
    if baseline:
        intentional = {"AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED"}
        values -= intentional
        expected_values -= intentional
    require(refs == descriptor["envSecretRefs"] and values == expected_values,
            "Live environment secret references differ from source.")
    return {"secrets": descriptor["secrets"], "envSecretRefs": dict(sorted(refs.items())),
            "envValueNames": sorted(values)}
