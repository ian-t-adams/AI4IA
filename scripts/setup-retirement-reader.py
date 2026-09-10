"""Plan, explicitly provision, and inspect the dedicated retirement reader.

No import/default/what-if/GitHub write path. This is not an azd hook or a reporter.
The bounded CLI transport is shared with the capacity reader, not its collectors.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid5

from _capacity_evidence import EvidenceError, az_command, run_bounded, strict_json

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/model-retirements.yml"
CATALOG = "infra/models.json"
ARM = "https://management.azure.com"
AUTH_API = "2022-04-01"
IDENTITY_API = "2023-01-31"
READER = "b24988ac-6180-42a0-ab88-20f7382dd24c"
MODELS_READ = "Microsoft.CognitiveServices/locations/models/read"
ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "api://AzureADTokenExchange"
MAX_BYTES = 4 * 1024 * 1024
STEPS = ("identity", "metadata_role", "group_reader", "model_reader", "federation")
VARIABLE_KEYS = {
    "REPORT_ENABLED", "REPORT_CLIENT_ID", "REPORT_TENANT_ID",
    "REPORT_SUBSCRIPTION_ID", "REPORT_RESOURCE_GROUP", "REPORT_ENV_NAME",
    "REPORT_CAPACITY_PROFILE", "REPORT_CLAUDE_ENABLED", "DEPLOY_CLIENT_ID",
}


class SetupError(ValueError):
    """A fixed diagnostic, never an upstream error body."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SetupError(message)


def object_value(value: object) -> dict:
    require(isinstance(value, dict), "Expected a complete JSON object; coverage unknown.")
    return value


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()


def guid(value: object) -> str:
    require(isinstance(value, str), "Expected an explicit UUID.")
    try:
        result = str(UUID(value))
    except ValueError:
        raise SetupError("Expected an explicit UUID.") from None
    require(value.lower() == result and UUID(result).int != 0, "Invalid UUID spelling.")
    return result


def same_id(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and actual.lower() == expected.lower()


def same_role(actual: object, expected: str) -> bool:
    # ARM can return a provider-root role reference after a subscription-qualified PUT.
    # Do not accept a reference qualified by a different subscription or provider.
    unqualified = "/providers/Microsoft.Authorization/roleDefinitions/" + expected.rsplit("/", 1)[1]
    return same_id(actual, expected) or same_id(actual, unqualified)


def resource(value: object, resource_id: str, resource_type: str) -> dict:
    row = object_value(value)
    require(
        same_id(row.get("id"), resource_id)
        and same_id(row.get("type"), resource_type)
        and same_id(row.get("name"), resource_id.rsplit("/", 1)[1]),
        "Resource ID/name/type collision; nothing will be adopted or replaced.",
    )
    object_value(row.get("properties"))
    return row


def cli_command(tool: str) -> list[str]:
    if tool == "az":
        return az_command()
    executable = shutil.which("gh")
    require(executable is not None, "GitHub CLI unavailable.")
    return [executable]


class Cli:
    def __init__(self, subscription: str, write_enabled: bool):
        self.subscription = subscription
        self.write_enabled = write_enabled
        self.attempted: list[str] = []
        self.calls = 0
        self.bytes = 0
        self.deadline = time.monotonic() + 600

    def call(self, tool: str, args: list[str]) -> object:
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        require(self.calls <= 192 and remaining > 0, "Setup observation budget exhausted.")
        result = run_bounded(cli_command(tool) + args, min(30, remaining), MAX_BYTES)
        self.bytes += len(result.body)
        require(self.bytes <= 32 * MAX_BYTES, "Setup response budget exhausted.")
        require(not result.warning, "CLI diagnostics were emitted; coverage unknown.")
        return strict_json(result.body)

    def get(self, path: str, api: str, query: dict | None = None) -> object:
        url = ARM + path + "?" + urlencode({"api-version": api, **(query or {})})
        return self.call("az", [
            "rest", "--method", "GET", "--url", url,
            "--subscription", self.subscription, "--only-show-errors", "--output", "json",
        ])

    def rows(self, path: str, api: str, query: dict | None = None) -> list[dict]:
        page = object_value(self.get(path, api, query))
        require(
            page.get("nextLink") in (None, "") and isinstance(page.get("value"), list),
            "Incomplete/paginated ARM inventory; coverage unknown. Do not apply or activate.",
        )
        rows = page["value"]
        require(len(rows) <= 4096, "ARM inventory exceeds the reviewed row bound.")
        ids = []
        for row in rows:
            row = object_value(row)
            require(isinstance(row.get("id"), str), "Missing resource identity in ARM inventory.")
            ids.append(row["id"].lower())
        require(len(ids) == len(set(ids)), "Duplicate resource identity in ARM inventory.")
        return sorted(rows, key=lambda row: row["id"].lower())

    def github(self, repo: str, endpoint: str) -> object:
        return self.call("gh", ["api", "--method", "GET", f"repos/{repo}{endpoint}"])

    def create(self, path: str, api: str, body: dict) -> object:
        require(self.write_enabled, "Writes require --apply without --what-if.")
        self.attempted.append(path)
        if "/roleAssignments/" in path:
            # scope is read-only in the assignment response; the request URL binds it.
            body = {"properties": {k: v for k, v in body["properties"].items() if k != "scope"}}
        with tempfile.TemporaryDirectory(prefix="ai4ia-reader-body-") as directory:
            payload = Path(directory) / "body.json"
            payload.write_text(json.dumps(body), encoding="utf-8")
            return self.call("az", [
                "rest", "--method", "PUT", "--url", ARM + path + "?api-version=" + api,
                "--headers", "Content-Type=application/json", "If-None-Match=*",
                "--body", "@" + str(payload), "--subscription", self.subscription,
                "--only-show-errors", "--output", "json",
            ])


@dataclass(frozen=True)
class Target:
    subscription: str
    tenant: str
    resource_group: str
    environment: str
    workload: str
    repository: str
    capacity_profile: str
    claude_enabled: str
    identity_resource_id: str | None

    def __post_init__(self) -> None:
        guid(self.subscription)
        guid(self.tenant)
        for value in (self.environment, self.workload):
            require(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?", value) is not None,
                "Workload/environment must be lowercase Azure naming tokens (1-20 characters).",
            )
        require(self.resource_group == f"rg-{self.workload}-{self.environment}",
                "Resource group must exactly match the workload/environment naming contract.")
        require(self.repository == "ian-t-adams/AI4IA", "Only the explicit AI4IA repository is supported.")
        require(self.claude_enabled in ("true", "false"), "Claude posture must be explicit true or false.")
        if self.identity_resource_id is not None:
            prefix = self.group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
            require(
                self.identity_resource_id.startswith(prefix)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}",
                                 self.identity_resource_id[len(prefix):]) is not None,
                "Existing identity must be an exact user-assigned identity ID in the workload group.",
            )

    @property
    def subscription_scope(self) -> str:
        return "/subscriptions/" + self.subscription

    @property
    def group(self) -> str:
        return self.subscription_scope + "/resourceGroups/" + self.resource_group

    @property
    def key(self) -> str:
        return f"ai4ia-retirement-reader/v1/{self.group}/{self.repository}"

    @property
    def role_id(self) -> str:
        return self.subscription_scope + "/providers/Microsoft.Authorization/roleDefinitions/" + str(
            uuid5(NAMESPACE_URL, self.key + "/models")
        )

    @property
    def identity(self) -> str:
        suffix = hashlib.sha256(self.key.encode()).hexdigest()[:12]
        return self.identity_resource_id or (
            self.group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
            + f"id-retirement-{self.workload}-{self.environment}-{suffix}"
        )

    @property
    def tags(self) -> dict:
        return {
            "managedBy": "ai4ia-retirement-reader", "workload": self.workload,
            "env": self.environment, "azd-env-name": self.environment,
            "repository": self.repository,
        }

    def intents(self, location: str, principal: str | None) -> dict:
        role_name = f"AI4IA retirement models {self.workload}-{self.environment}-" + self.role_id[-12:]
        result = {
            "identity": {
                "id": self.identity, "api": IDENTITY_API,
                "body": {"location": location, "tags": self.tags},
            },
            "metadata_role": {
                "id": self.role_id, "api": AUTH_API,
                "body": {"properties": {
                    "roleName": role_name, "type": "CustomRole",
                    "description": f"Dedicated model-offering metadata for {self.repository}; {self.group}.",
                    "permissions": [{
                        "actions": [MODELS_READ], "notActions": [],
                        "dataActions": [], "notDataActions": [],
                    }],
                    "assignableScopes": [self.subscription_scope],
                }},
            },
            "federation": {
                "id": self.identity + "/federatedIdentityCredentials/github-main",
                "api": IDENTITY_API,
                "body": {"properties": {
                    "issuer": ISSUER, "subject": f"repo:{self.repository}:ref:refs/heads/main",
                    "audiences": [AUDIENCE],
                }},
            },
        }
        for name, scope, role in (
            ("group_reader", self.group,
             self.subscription_scope + "/providers/Microsoft.Authorization/roleDefinitions/" + READER),
            ("model_reader", self.subscription_scope, self.role_id),
        ):
            assignment_id = str(uuid5(NAMESPACE_URL, self.key + self.identity + "/" + name))
            result[name] = {
                "id": scope + "/providers/Microsoft.Authorization/roleAssignments/" + assignment_id,
                "api": AUTH_API,
                "body": {"properties": {
                    "principalId": principal, "principalType": "ServicePrincipal",
                    "roleDefinitionId": role, "scope": scope,
                }},
            }
        return result


def source_contract(cli: Cli, target: Target) -> tuple[dict, dict, dict]:
    repo = object_value(cli.github(target.repository, ""))
    require(
        repo.get("full_name") == target.repository and repo.get("default_branch") == "main"
        and repo.get("archived") is False and repo.get("disabled") is False,
        "GitHub repository/default-branch context does not match the approved target.",
    )
    sources = {}
    bodies = {}
    for path in (WORKFLOW, CATALOG):
        remote = object_value(cli.github(target.repository, "/contents/" + path + "?ref=main"))
        require(remote.get("encoding") == "base64" and isinstance(remote.get("content"), str),
                "Current main source is unavailable.")
        try:
            body = base64.b64decode("".join(remote["content"].split()), validate=True)
            text = body.decode("utf-8").replace("\r\n", "\n")
        except (ValueError, UnicodeError):
            raise SetupError("Invalid current-main source encoding.") from None
        local = (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")
        require(text == local, "Local workflow/catalog differs from current main; refresh before setup.")
        sources[path] = hashlib.sha256(text.encode()).hexdigest()
        bodies[path] = text
    sources["setup"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    workflow = bodies[WORKFLOW]
    matches = re.findall(r"(?m)^          (\w+): \$\{\{ vars\.(\w+) \}\}$", workflow)
    variables = dict(matches)
    require(
        len(matches) == len(variables) and set(variables) == VARIABLE_KEYS
        and len(set(variables.values())) == len(variables),
        "Unsupported reader workflow variable contract.",
    )
    profiles = re.findall(r'"\$REPORT_CAPACITY_PROFILE" != "([a-z]+)"', workflow)
    require(bool(profiles) and len(profiles) == len(set(profiles)), "Unknown profile guard.")
    require(target.capacity_profile in profiles, "Capacity profile is not accepted by the current main workflow.")
    require(
        re.search(r"(?m)^\s+environment:", workflow) is None
        and "repo:" not in workflow
        and "github.ref == 'refs/heads/main'" in workflow
        and 'if [[ "$REPORT_ENABLED" != "true" ]]' in workflow,
        "Unsupported branch/activation contract; no environment OIDC adoption is allowed.",
    )
    config: dict[str, str | None] = dict.fromkeys(variables.values())
    seen = set()
    total = None
    for page_number in range(1, 9):
        page = object_value(cli.github(
            target.repository, f"/actions/variables?per_page=100&page={page_number}",
        ))
        require(type(page.get("total_count")) is int and isinstance(page.get("variables"), list),
                "GitHub variable inventory is incomplete.")
        if total is None:
            total = page["total_count"]
        require(0 <= total <= 800 and page["total_count"] == total, "GitHub variable inventory changed.")
        rows = page["variables"]
        require(len(rows) <= 100, "Unexpected GitHub variable page size.")
        for row in rows:
            row = object_value(row)
            name = row.get("name")
            require(isinstance(name, str) and name not in seen and isinstance(row.get("value"), str),
                    "Duplicate/malformed GitHub variable.")
            seen.add(name)
            if name in config:
                config[name] = row["value"]
        require(len(seen) <= total, "GitHub variable inventory has contradictory coverage.")
        if len(seen) == total:
            break
        require(bool(rows), "GitHub variable inventory ended early.")
    require(len(seen) == total, "GitHub variable inventory is partial.")
    guid(config[variables["DEPLOY_CLIENT_ID"]])
    require(config[variables["REPORT_ENABLED"]] in (None, "", "false"),
            "Reporter is already enabled or has an ambiguous flag; setup never edits an active reader.")
    return {"sources": sources, "variables": variables, "configuration": config}, strict_json(
        bodies[CATALOG].encode()
    ), repo


def validate_identity(row: dict, target: Target, location: str, deploy_client: str) -> None:
    resource(row, target.identity, "Microsoft.ManagedIdentity/userAssignedIdentities")
    require(row.get("location") == location, "Identity location does not match the workload group.")
    tags = object_value(row.get("tags"))
    require(all(tags.get(k) == v for k, v in target.tags.items()),
            "Identity is not tagged as this dedicated reader; it will not be adopted or retagged.")
    props = row["properties"]
    client, principal = guid(props.get("clientId")), guid(props.get("principalId"))
    require(client != principal and guid(props.get("tenantId")) == target.tenant,
            "Identity client/principal/tenant binding is invalid.")
    require(client != guid(deploy_client) and principal != guid(deploy_client),
            "The reader must not share the deployment identity.")
    require(props.get("provisioningState", "Succeeded") == "Succeeded",
            "Identity provisioning is incomplete.")


def validate_step(name: str, row: dict, intent: dict) -> None:
    expected_type = {
        "metadata_role": "Microsoft.Authorization/roleDefinitions",
        "group_reader": "Microsoft.Authorization/roleAssignments",
        "model_reader": "Microsoft.Authorization/roleAssignments",
        "federation": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials",
    }[name]
    resource(row, intent["id"], expected_type)
    expected = intent["body"]["properties"]
    props = row["properties"]
    for key, value in expected.items():
        if key == "roleDefinitionId":
            require(same_role(props.get(key), value), "Assignment role reference mismatch.")
        elif key in ("scope", "principalId"):
            require(same_id(props.get(key), value), "Assignment scope/principal/role mismatch.")
        else:
            require(props.get(key) == value, f"Unexpected {name} {key}; refusing collision/overprivilege.")
    if name in ("group_reader", "model_reader"):
        require(all(props.get(key) in (None, "") for key in (
            "condition", "conditionVersion", "delegatedManagedIdentityResourceId",
        )), "Conditional/delegated assignment is not the reviewed reader grant.")
    if name == "federation":
        require(set(props) == set(expected), "Unreviewed federation properties or matching expression.")


def account_context(cli: Cli, target: Target, catalog: dict) -> list[dict]:
    naming = object_value(catalog.get("naming"))
    token = naming.get("foundryToken")
    require(isinstance(token, str) and re.fullmatch(r"[a-z0-9-]{1,30}", token) is not None,
            "Unknown catalog account naming.")
    models = catalog.get("catalog")
    require(isinstance(models, list), "Unknown catalog model inventory.")
    regions = sorted({
        deployment["region"] for model in models
        if target.claude_enabled == "true" or model["format"] != "Anthropic"
        for deployment in model["deployments"]
    })
    require(0 < len(regions) <= 8, "Unsupported catalog regional coverage.")
    rows = cli.rows(target.group + "/providers/Microsoft.CognitiveServices/accounts", "2024-10-01")
    context = []
    for region in regions:
        require(isinstance(region, str) and re.fullmatch(r"[a-z0-9]{1,32}", region) is not None,
                "Malformed catalog region.")
        pattern = re.compile(rf"mf-{re.escape(token)}-{target.environment}-{region}-[a-z0-9]{{13}}")
        selected = [r for r in rows if isinstance(r.get("name"), str) and pattern.fullmatch(r["name"])]
        require(len(selected) == 1, "Missing/ambiguous catalog account context; coverage unknown.")
        row = selected[0]
        resource(row, target.group + "/providers/Microsoft.CognitiveServices/accounts/" + row["name"],
                 "Microsoft.CognitiveServices/accounts")
        tags = object_value(row.get("tags"))
        require(
            row.get("kind") == "AIServices" and row.get("location") == region
            and all(tags.get(k) == v for k, v in {
                "env": target.environment, "azd-env-name": target.environment,
                "workload": target.workload, "managedBy": "azd-bicep",
            }.items()) and row["properties"].get("provisioningState") == "Succeeded",
            "Account scope/environment/provisioning context does not match.",
        )
        context.append({"id": row["id"], "region": region, "version": digest(row)})
    return context


def observe(cli: Cli, target: Target) -> dict:
    github, catalog, _ = source_contract(cli, target)
    account = object_value(cli.call("az", [
        "account", "show", "--subscription", target.subscription, "--output", "json",
        "--only-show-errors",
    ]))
    require(
        same_id(account.get("id"), target.subscription) and same_id(account.get("tenantId"), target.tenant)
        and account.get("state") == "Enabled" and account.get("environmentName") == "AzureCloud",
        "Azure subscription/tenant/cloud context does not match; no subscription selection is performed.",
    )
    group = object_value(cli.get(target.group, "2024-03-01"))
    require(same_id(group.get("id"), target.group) and group.get("name") == target.resource_group,
            "Resource group ID/name mismatch.")
    group_tags = object_value(group.get("tags"))
    require(all(group_tags.get(k) == v for k, v in {
        "env": target.environment, "azd-env-name": target.environment,
        "workload": target.workload, "managedBy": "azd-bicep",
    }.items()), "Workload resource group ownership tags do not match.")
    require(object_value(group.get("properties")).get("provisioningState") == "Succeeded",
            "Workload resource group is not fully provisioned.")
    location = group.get("location")
    require(isinstance(location, str) and re.fullmatch(r"[a-z0-9]{1,32}", location) is not None,
            "Invalid resource group location.")
    accounts = account_context(cli, target, object_value(catalog))
    identities = cli.rows(target.group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities", IDENTITY_API)
    for row in identities:
        name = row.get("name")
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}", name) is not None,
                "Malformed identity inventory name.")
        resource(row, target.group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/" + name,
                 "Microsoft.ManagedIdentity/userAssignedIdentities")
    identity = next((r for r in identities if same_id(r["id"], target.identity)), None)
    deploy_client = github["configuration"][github["variables"]["DEPLOY_CLIENT_ID"]]
    if identity is not None:
        validate_identity(identity, target, location, deploy_client)
    principal = identity["properties"]["principalId"] if identity else None
    intents = target.intents(location, principal)
    roles = cli.rows(target.subscription_scope + "/providers/Microsoft.Authorization/roleDefinitions",
                     AUTH_API, {"$filter": "type eq 'CustomRole'"})
    role = next((r for r in roles if same_id(r["id"], target.role_id)), None)
    for row in roles:
        if object_value(row.get("properties")).get("roleName") == intents["metadata_role"]["body"]["properties"]["roleName"]:
            require(same_id(row["id"], target.role_id), "Custom role name is owned by a different role ID.")
    if role is not None:
        validate_step("metadata_role", role, intents["metadata_role"])
    assignments: dict[str, dict] = {}
    # Scope reads prove literal principalId, not CLI directory/name resolution.
    # principalId includes ancestors/descendants; assignedTo additionally exposes group grants.
    queries = [(target.subscription_scope, "atScope()"), (target.group, "atScope()")]
    if principal:
        queries += [
            (target.subscription_scope, f"principalId eq '{principal}'"),
            (target.subscription_scope, f"assignedTo('{principal}')"),
        ]
    for scope, query in queries:
        rows = cli.rows(scope + "/providers/Microsoft.Authorization/roleAssignments", AUTH_API, {"$filter": query})
        for row in rows:
            props = object_value(row.get("properties"))
            if query.startswith("principalId eq"):
                require(same_id(props.get("principalId"), principal), "Contradictory principal-filtered role inventory.")
            known = any(same_id(row["id"], intents[name]["id"]) for name in ("group_reader", "model_reader"))
            selected = query.startswith("assignedTo") or (principal and same_id(props.get("principalId"), principal))
            if selected or known:
                key = row["id"].lower()
                require(key not in assignments or assignments[key] == row, "Contradictory assignment reads.")
                assignments[key] = row
    allowed = {intents[name]["id"].lower() for name in ("group_reader", "model_reader")}
    require(set(assignments) <= allowed,
            "Extra/inherited/group role assignments found; overprivileged or unreviewed. Never auto-revoke.")
    found = {"identity": identity, "metadata_role": role}
    for name in ("group_reader", "model_reader"):
        row = assignments.get(intents[name]["id"].lower())
        if row is not None:
            require(principal is not None, "Role assignment exists without the owned identity.")
            validate_step(name, row, intents[name])
        found[name] = row
    federation = None
    if identity:
        rows = cli.rows(target.identity + "/federatedIdentityCredentials", IDENTITY_API)
        require(len(rows) <= 1, "Additional federated credentials exist; no trust will be replaced.")
        if rows:
            federation = rows[0]
            validate_step("federation", federation, intents["federation"])
    found["federation"] = federation
    wanted = {
        "REPORT_CLIENT_ID": identity["properties"]["clientId"] if identity else None,
        "REPORT_TENANT_ID": target.tenant, "REPORT_SUBSCRIPTION_ID": target.subscription,
        "REPORT_RESOURCE_GROUP": target.resource_group, "REPORT_ENV_NAME": target.environment,
        "REPORT_CAPACITY_PROFILE": target.capacity_profile, "REPORT_CLAUDE_ENABLED": target.claude_enabled,
    }
    for key, value in wanted.items():
        actual = github["configuration"][github["variables"][key]]
        require(actual in (None, "", value), "Existing dedicated report configuration targets a different reader/scope.")
    return {
        "schema": "retirement-reader-setup/v1", "target": asdict(target), "github": github,
        "context": {"group_id": target.group, "group_version": digest(group), "accounts": accounts},
        "intents": intents, "resources": found, "wanted": wanted,
    }


def configuration_commands(plan: dict) -> list[str]:
    return [
        f"gh variable set '{plan['github']['variables'][key]}' --repo '{plan['target']['repository']}' --body '{value}'"
        for key, value in plan["wanted"].items()
    ]


def output(plan: dict, mode: str, activation: bool = False) -> dict:
    complete = all(plan["resources"].values())
    return {
        "mode": mode, "plan_sha256": digest(plan),
        "target": plan["target"], "sources": plan["github"]["sources"],
        "context": plan["context"],
        "steps": [{
            "step": name, "state": "verified" if plan["resources"][name] else "create",
            **plan["intents"][name],
            "observed_version": digest(plan["resources"][name]) if plan["resources"][name] else None,
        } for name in STEPS],
        "configuration_commands": configuration_commands(plan) if complete else [],
        "activation_command": (
            f"gh variable set '{plan['github']['variables']['REPORT_ENABLED']}'"
            f" --repo '{plan['target']['repository']}' --body 'true'"
        ) if activation else None,
        "notice": (
            "No GitHub setting was changed. Plan approval is scope binding, not proof of human authority. "
            "Metadata inspection is not OIDC/login/report permission proof. No model or quota calls. "
            "Costs are not estimated; activation can incur GitHub Actions/artifact retention charges. "
            "RBAC outside this subscription/its ancestors and non-ARM directory authority are not certified."
        ),
    }


def execute(cli: Cli, target: Target, args: argparse.Namespace) -> dict:
    plan = observe(cli, target)
    if args.apply:
        require(digest(plan) == args.approve_plan, "Stale/wrong approved plan digest; generate and review a fresh plan.")
    if args.what_if:
        return output(plan, "what-if: no mutations")
    if args.apply:
        fresh = observe(cli, target)
        require(digest(fresh) == digest(plan), "Scope changed during fresh approval reads; no writes authorized.")
        plan = fresh
        for name in STEPS:
            if plan["resources"][name] is not None:
                continue
            # The full post-write readback is also the fresh observation immediately
            # before the next create; no operator prompt or unrelated work intervenes.
            intent = plan["intents"][name]
            result = object_value(cli.create(intent["id"], intent["api"], intent["body"]))
            if name == "identity":
                validate_identity(result, target, intent["body"]["location"],
                                  plan["github"]["configuration"][plan["github"]["variables"]["DEPLOY_CLIENT_ID"]])
            else:
                validate_step(name, result, intent)
            fresh = observe(cli, target)
            require(fresh["resources"][name] is not None, "Create readback missing; state is partial/unknown.")
            require(fresh["github"] == plan["github"] and fresh["context"] == plan["context"],
                    "Scope/source changed during create; state is partial/unknown.")
            for previous in STEPS:
                if plan["resources"][previous] is not None:
                    require(fresh["resources"][previous] == plan["resources"][previous],
                            "Previously observed resource changed during create; stop and re-plan.")
                elif previous != name:
                    require(fresh["resources"][previous] is None, "Unexpected concurrent resource appeared.")
            plan = fresh
        require(all(plan["resources"].values()), "Setup incomplete; do not configure or activate.")
        return output(plan, "Azure setup read back; reporter still disabled")
    if args.verify_configuration:
        require(all(plan["resources"].values()), "Setup is incomplete; activation command withheld.")
        for key, value in plan["wanted"].items():
            require(plan["github"]["configuration"][plan["github"]["variables"][key]] == value,
                    "Dedicated configuration is incomplete; activation command withheld.")
        fresh = observe(cli, target)
        require(digest(fresh) == digest(plan), "Configuration changed during verification.")
        return output(plan, "Pre-activation metadata verified; manual approval still required", activation=True)
    return output(plan, "read-only plan: no mutations")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("subscription", "tenant", "resource-group", "environment", "workload",
                 "repository", "capacity-profile", "claude-enabled"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--identity-resource-id", help="Exact, already owned dedicated UAMI; never a deployment identity.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--verify-configuration", action="store_true",
                       help="Read-only readiness check; emits, but does not execute, the manual activation command.")
    parser.add_argument("--approve-plan", help="SHA-256 printed by a reviewed, unchanged default plan.")
    parser.add_argument("--what-if", action="store_true", help="Never mutate, including when combined with --apply.")
    args = parser.parse_args()
    cli = None
    try:
        require(
            (args.apply and isinstance(args.approve_plan, str)
             and re.fullmatch("[0-9a-f]{64}", args.approve_plan) is not None)
            or (not args.apply and args.approve_plan is None),
            "--apply requires --approve-plan <sha256>; approval is invalid without --apply.",
        )
        target = Target(**{name: getattr(args, name) for name in Target.__dataclass_fields__})
        cli = Cli(target.subscription, args.apply and not args.what_if)
        result = execute(cli, target, args)
        print(json.dumps(result, indent=2))
        return 0
    except (SetupError, EvidenceError, OSError, KeyboardInterrupt) as exc:
        message = str(exc) if isinstance(exc, SetupError) else "CLI/local evidence unavailable; coverage unknown."
        if isinstance(exc, KeyboardInterrupt):
            message = "Interrupted; Azure writes may be partial/unknown."
        print(json.dumps({
            "status": "blocked: unknown or partial", "error": message,
            "attempted_resource_ids": cli.attempted if cli else [],
            "activation_command": None,
            "recovery": "Do not activate. Keep partial resources; generate a new plan. No automatic retry, revoke, or rollback.",
        }), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
