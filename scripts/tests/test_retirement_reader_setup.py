"""Run the real setup CLI/transport with offline az/gh subprocesses and durable state."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
setup = load_script("retirement_reader_setup", ROOT / "scripts" / "setup-retirement-reader.py", register=True)
SUB = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
PRINCIPAL = "44444444-4444-4444-8444-444444444444"
DEPLOY_CLIENT = "55555555-5555-4555-8555-555555555555"
GROUP_PRINCIPAL = "66666666-6666-4666-8666-666666666666"
REPO = "ian-t-adams/AI4IA"

RUNNER = """
import importlib.util
import sys
from pathlib import Path
script, stub, *arguments = sys.argv[1:]
sys.path.insert(0, str(Path(script).parent))
spec = importlib.util.spec_from_file_location("reader_under_test", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.cli_command = lambda tool: [sys.executable, stub, tool]
sys.argv = [script, *arguments]
raise SystemExit(module.main())
"""

STUB = r"""
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

tool, *args = sys.argv[1:]
root = Path(os.environ["READER_STUB_DIRECTORY"])
store = root / "state.json"
state = json.loads(store.read_text())
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
state["call_count"] = state.get("call_count", 0) + 1
key = " ".join([tool, *args])
if state.get("drift_at") == state["call_count"]:
    state["group"]["etag"] += "-changed"
if state.get("fail_contains") and state["fail_contains"] in key:
    print("PRIVATE-UPSTREAM-ERROR", file=sys.stderr)
    sys.exit(23)
if state.get("warn_contains") and state["warn_contains"] in key:
    print("PRIVATE-UPSTREAM-WARNING", file=sys.stderr)
def save():
    store.write_text(json.dumps(state))
def value(flag):
    return args[args.index(flag) + 1]
def response(result):
    save()
    print(json.dumps(result))
    sys.exit(0)
if tool == "gh":
    assert args[:3] == ["api", "--method", "GET"], args
    endpoint = args[3]
    assert endpoint.startswith("repos/ian-t-adams/AI4IA"), args
    if endpoint.endswith(REPO := "repos/ian-t-adams/AI4IA"):
        response(state["repo"])
    if "/contents/" in endpoint:
        path = endpoint.split("/contents/", 1)[1].split("?")[0]
        response({"encoding": "base64", "content": base64.b64encode(state["sources"][path].encode()).decode()})
    if "/actions/variables?" in endpoint:
        page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
        rows = [{"name": k, "value": v} for k, v in state["variables"].items()]
        response({"total_count": len(rows), "variables": rows[(page-1)*100:page*100]})
    raise AssertionError(args)
assert tool == "az"
assert value("--subscription") == state["subscription"]
if args[:2] == ["account", "show"]:
    response(state["account"])
assert args[0] == "rest"
url = urlsplit(value("--url"))
assert url.scheme == "https" and url.netloc == "management.azure.com"
path, method = url.path, value("--method")
if method == "PUT":
    state["writes"] = state.get("writes", 0) + 1
    assert path in state["allowed_creates"], path
    assert "If-None-Match=*" in args
    assert path not in state["resources"], "existing resource overwritten"
    body = json.loads(Path(value("--body").removeprefix("@")).read_text())
    state.setdefault("bodies", []).append({"path": path, "body": body})
    if state.get("fail_write") == state["writes"]:
        save()
        print("PRIVATE-PARTIAL-WRITE", file=sys.stderr)
        sys.exit(24)
    name = path.rsplit("/", 1)[1]
    if "/federatedIdentityCredentials/" in path:
        row = {"id": path, "name": name,
               "type": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials", **body}
    elif "/userAssignedIdentities/" in path:
        row = {"id": path, "name": name, "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
               **body, "properties": {"clientId": state["client"], "principalId": state["principal"],
                                      "tenantId": state["tenant"]}}
    elif "/roleDefinitions/" in path:
        row = {"id": path, "name": name, "type": "Microsoft.Authorization/roleDefinitions", **body}
    elif "/roleAssignments/" in path:
        row = {"id": path, "name": name, "type": "Microsoft.Authorization/roleAssignments", **body}
        row["properties"]["scope"] = path.split("/providers/Microsoft.Authorization/roleAssignments/")[0]
        if state.get("assign_client"):
            row["properties"]["principalId"] = state["client"]
    else:
        raise AssertionError(path)
    row["etag"] = "fixture-version-1"
    if not state.get("drop_write") == state["writes"]:
        state["resources"][path] = row
    if state.get("fail_after_write") == state["writes"]:
        save()
        sys.exit(25)
    response(row)
assert method == "GET"
if state.get("malformed_contains") and state["malformed_contains"] in key:
    save()
    print('{"value":null}')
    sys.exit(0)
if path == state["group"]["id"]:
    response(state["group"])
if path.endswith("/Microsoft.CognitiveServices/accounts"):
    rows = state["accounts"]
elif path.endswith("/Microsoft.ManagedIdentity/userAssignedIdentities"):
    rows = [v for v in state["resources"].values()
            if v["type"] == "Microsoft.ManagedIdentity/userAssignedIdentities"]
elif path.endswith("/roleDefinitions"):
    rows = [v for v in state["resources"].values() if v["type"] == "Microsoft.Authorization/roleDefinitions"]
elif path.endswith("/federatedIdentityCredentials"):
    rows = [v for v in state["resources"].values()
            if v["type"].endswith("/federatedIdentityCredentials") and v["id"].startswith(path + "/")]
elif path.endswith("/roleAssignments"):
    scope = path.removesuffix("/providers/Microsoft.Authorization/roleAssignments")
    query = parse_qs(url.query)["$filter"][0]
    rows = [v for v in state["resources"].values() if v["type"] == "Microsoft.Authorization/roleAssignments"]
    if query == "atScope()":
        rows = [v for v in rows if scope.startswith(v["properties"]["scope"])
                or v["properties"]["scope"].startswith("/providers/Microsoft.Management/")]
    elif query.startswith("principalId eq"):
        principal = query.split("'")[1]
        rows = [v for v in rows if v["properties"]["principalId"] == principal]
    elif query.startswith("assignedTo("):
        principal = query.split("'")[1]
        rows = [v for v in rows if v["properties"]["principalId"] in [principal, *state.get("groups", [])]]
    else:
        raise AssertionError(query)
else:
    raise AssertionError(path)
result = {"value": rows}
if state.get("page_contains") and state["page_contains"] in key:
    result["nextLink"] = "https://management.azure.com/unreviewed-continuation"
response(result)
"""


class Fixture:
    def __init__(self, directory: Path):
        self.directory = directory
        self.target = setup.Target(SUB, TENANT, "rg-demo-example", "example", "demo", REPO,
                                   "baseline", "false", None)
        self.intents = self.target.intents("eastus2", PRINCIPAL)
        tags = {"env": "example", "azd-env-name": "example", "workload": "demo", "managedBy": "azd-bicep"}
        sources = {p: (ROOT / p).read_text(encoding="utf-8") for p in (setup.WORKFLOW, setup.CATALOG)}
        catalog = json.loads(sources[setup.CATALOG])
        regions = {
            d["region"] for m in catalog["catalog"] if m["format"] != "Anthropic"
            for d in m["deployments"]
        }
        accounts = []
        for region in sorted(regions):
            name = f"mf-{catalog['naming']['foundryToken']}-example-{region}-abcdefghijklm"
            accounts.append({
                "id": self.target.group + "/providers/Microsoft.CognitiveServices/accounts/" + name,
                "name": name, "type": "Microsoft.CognitiveServices/accounts",
                "kind": "AIServices", "location": region, "tags": tags.copy(),
                "properties": {"provisioningState": "Succeeded"}, "etag": "account-v1",
            })
        self.state = {
            "subscription": SUB, "tenant": TENANT, "client": CLIENT, "principal": PRINCIPAL,
            "account": {"id": SUB, "tenantId": TENANT, "state": "Enabled", "environmentName": "AzureCloud"},
            "group": {"id": self.target.group, "name": self.target.resource_group, "tags": tags,
                      "location": "eastus2", "properties": {"provisioningState": "Succeeded"}, "etag": "rg-v1"},
            "repo": {"full_name": REPO, "default_branch": "main", "archived": False, "disabled": False},
            "sources": sources, "variables": {"AZURE_CLIENT_ID": DEPLOY_CLIENT}, "accounts": accounts,
            "resources": {}, "allowed_creates": [i["id"] for i in self.intents.values()],
        }
        (directory / "runner.py").write_text(RUNNER, encoding="utf-8")
        (directory / "cli.py").write_text(STUB, encoding="utf-8")
        self.arguments = [
            "--subscription", SUB, "--tenant", TENANT, "--resource-group", self.target.resource_group,
            "--environment", "example", "--workload", "demo", "--repository", REPO,
            "--capacity-profile", "baseline", "--claude-enabled", "false",
        ]

    def run(self, *extra: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
        store = self.directory / "state.json"
        store.write_text(json.dumps(self.state), encoding="utf-8")
        log = self.directory / "calls.jsonl"
        log.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, str(self.directory / "runner.py"), str(ROOT / "scripts" / "setup-retirement-reader.py"),
             str(self.directory / "cli.py"), *self.arguments, *extra],
            env={**os.environ, "READER_STUB_DIRECTORY": str(self.directory)},
            cwd=ROOT, text=True, capture_output=True, timeout=120, check=False,
        )
        self.state = json.loads(store.read_text(encoding="utf-8"))
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    def plan(self) -> dict:
        result, _ = self.run()
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def apply(self) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
        plan = self.plan()
        return self.run("--apply", "--approve-plan", plan["plan_sha256"])

    def complete(self) -> dict:
        result, _ = self.apply()
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def configure(self) -> None:
        workflow = yaml.safe_load(self.state["sources"][setup.WORKFLOW])
        env = workflow["jobs"]["activation"]["steps"][0]["env"]
        values = {
            "REPORT_CLIENT_ID": CLIENT, "REPORT_TENANT_ID": TENANT, "REPORT_SUBSCRIPTION_ID": SUB,
            "REPORT_RESOURCE_GROUP": self.target.resource_group, "REPORT_ENV_NAME": "example",
            "REPORT_CAPACITY_PROFILE": "baseline", "REPORT_CLAUDE_ENABLED": "false",
        }
        for key, value in values.items():
            variable = env[key].removeprefix("${{ vars.").removesuffix(" }}")
            self.state["variables"][variable] = value

    def row(self, name: str) -> dict:
        return self.state["resources"][self.intents[name]["id"]]


class ReaderSetupExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="reader-setup-test-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(Path(self.temp.name))

    def assert_no_mutations(self, calls: list[list[str]]) -> None:
        self.assertFalse([call for call in calls if "PUT" in call or "set" in call or "create" in call])
        self.assertTrue(all(call[:4] == ["gh", "api", "--method", "GET"] for call in calls if call[0] == "gh"))

    def assert_blocked(self, result: subprocess.CompletedProcess, calls: list[list[str]], text: str = "") -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assert_no_mutations(calls)
        self.assertIn(text, result.stderr)
        self.assertNotIn("PRIVATE-", result.stderr + result.stdout)
        self.assertNotIn("--body 'true'", result.stdout)

    def test_import_has_no_cli_or_file_side_effects(self) -> None:
        # The import above precedes the test stub setup. This separate process also
        # has no az/gh on PATH, so an import cannot accidentally depend on credentials.
        result = subprocess.run(
            [sys.executable, "-c",
             "import runpy; runpy.run_path('scripts/setup-retirement-reader.py', run_name='import_only')"],
            env={**os.environ, "PATH": "", "PYTHONPATH": str(ROOT / "scripts")},
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_default_plan_and_what_if_read_but_never_write(self) -> None:
        for extra in ((), ("--what-if",)):
            with self.subTest(extra=extra):
                result, calls = self.fixture.run(*extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_no_mutations(calls)
                self.assertGreater(len(calls), 10)
                data = json.loads(result.stdout)
                self.assertEqual([s["state"] for s in data["steps"]], ["create"] * 5)
                self.assertIsNone(data["activation_command"])
        plan = self.fixture.plan()
        result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"], "--what-if")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_mutations(calls)

    def test_apply_requires_digest_before_any_cli_call(self) -> None:
        for extra in (("--apply",), ("--apply", "--approve-plan", "yes"), ("--approve-plan", "a" * 64)):
            with self.subTest(extra=extra):
                result, calls = self.fixture.run(*extra)
                self.assert_blocked(result, calls, "approval" if "--apply" not in extra else "--apply")
                self.assertEqual(calls, [])

    def test_apply_creates_only_five_exact_resources_and_is_idempotent(self) -> None:
        result, calls = self.fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = [call for call in calls if "PUT" in call]
        self.assertEqual(len(writes), 5)
        self.assertEqual(set(self.fixture.state["resources"]), set(self.fixture.state["allowed_creates"]))
        role = self.fixture.row("metadata_role")["properties"]
        self.assertEqual(role["permissions"], [{
            "actions": ["Microsoft.CognitiveServices/locations/models/read"],
            "notActions": [], "dataActions": [], "notDataActions": [],
        }])
        self.assertEqual(role["assignableScopes"], ["/subscriptions/" + SUB])
        group_grant = self.fixture.row("group_reader")["properties"]
        self.assertEqual(group_grant["scope"], self.fixture.target.group)
        self.assertTrue(group_grant["roleDefinitionId"].endswith("/" + setup.READER))
        for name in ("group_reader", "model_reader"):
            self.assertEqual(self.fixture.row(name)["properties"]["principalId"], PRINCIPAL)
            self.assertNotEqual(self.fixture.row(name)["properties"]["principalId"], CLIENT)
        trust = self.fixture.row("federation")["properties"]
        self.assertEqual(trust, {
            "issuer": "https://token.actions.githubusercontent.com",
            "subject": "repo:ian-t-adams/AI4IA:ref:refs/heads/main",
            "audiences": ["api://AzureADTokenExchange"],
        })
        data = json.loads(result.stdout)
        self.assertIsNone(data["activation_command"])
        self.assertEqual(len(data["configuration_commands"]), 7)
        self.assertTrue(any(CLIENT in c for c in data["configuration_commands"]))
        self.assertFalse(any(PRINCIPAL in c or "REPORT_ENABLED" in c for c in data["configuration_commands"]))
        self.assertTrue(all("--subscription" in c for c in calls if c[0] == "az"))
        self.assertFalse(any("models" in c or "usages" in c or "login" in c or "ad" in c for c in calls))
        result, calls = self.fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_mutations(calls)

    def test_stale_plan_and_fresh_read_drift_stop_before_writes(self) -> None:
        plan = self.fixture.plan()
        self.fixture.state["group"]["etag"] = "rg-v2"
        result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"])
        self.assert_blocked(result, calls, "Stale/wrong")
        plan = self.fixture.plan()
        # Each observe begins with repository metadata. Drift during the second
        # full observation after approval must not turn into a create.
        start = self.fixture.state["call_count"]
        self.fixture.state["drift_at"] = start + 15
        result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"])
        self.assert_blocked(result, calls, "changed")

    def test_bad_operator_scope_rejected_before_reads(self) -> None:
        cases = [
            ("--subscription", "not-a-uuid"), ("--tenant", "00000000-0000-0000-0000-000000000000"),
            ("--repository", "attacker/AI4IA"), ("--resource-group", "rg-other-example"),
            ("--workload", "demo;az login"), ("--environment", "../example"),
            ("--identity-resource-id", "/subscriptions/" + TENANT + "/resourceGroups/rg-demo-example/"
             "providers/Microsoft.ManagedIdentity/userAssignedIdentities/reader"),
        ]
        for flag, value in cases:
            with self.subTest(flag=flag):
                result, calls = self.fixture.run(flag, value)
                self.assert_blocked(result, calls)
                self.assertEqual(calls, [])

    def test_failed_scope_source_and_identity_reads_never_mean_missing(self) -> None:
        for marker in (
            "account show", "/resourceGroups/rg-demo-example?", "Microsoft.CognitiveServices",
            "userAssignedIdentities", "roleDefinitions", "roleAssignments", "actions/variables",
        ):
            with self.subTest(marker=marker):
                self.fixture.state["fail_contains"] = marker
                result, calls = self.fixture.run()
                self.assert_blocked(result, calls, "coverage unknown")
        self.fixture.state.pop("fail_contains")
        control = self.fixture.plan()
        self.assertEqual(len(control["steps"]), 5)

    def test_unknown_or_partial_inventory_never_activates(self) -> None:
        for field in ("warn_contains", "page_contains", "malformed_contains"):
            with self.subTest(field=field):
                self.fixture.state[field] = "userAssignedIdentities"
                result, calls = self.fixture.run()
                self.assert_blocked(result, calls)
                self.fixture.state.pop(field)
        self.fixture.plan()

    def test_observed_wrong_subscription_tenant_or_group_is_rejected(self) -> None:
        original = copy.deepcopy(self.fixture.state)
        for scope, key, value in (
            ("account", "id", TENANT), ("account", "tenantId", SUB),
            ("account", "environmentName", "AzureUSGovernment"),
            ("group", "id", self.fixture.target.group + "-other"),
            ("group", "tags", {"env": "unrelated"}),
        ):
            with self.subTest(scope=scope, key=key):
                self.fixture.state = copy.deepcopy(original)
                self.fixture.state[scope][key] = value
                result, calls = self.fixture.run()
                self.assert_blocked(result, calls)

    def test_current_main_workflow_repo_and_profile_must_match(self) -> None:
        original = copy.deepcopy(self.fixture.state)
        for change in ("main", "repo", "profile"):
            with self.subTest(change=change):
                self.fixture.state = copy.deepcopy(original)
                extra = []
                if change == "main":
                    self.fixture.state["sources"][setup.WORKFLOW] += "\n# remote change\n"
                elif change == "repo":
                    self.fixture.state["repo"]["default_branch"] = "development"
                else:
                    extra = ["--capacity-profile", "guessed-profile"]
                result, calls = self.fixture.run(*extra)
                self.assert_blocked(result, calls)

    def test_existing_identity_requires_owned_tags_and_exact_ids(self) -> None:
        self.fixture.complete()
        original = copy.deepcopy(self.fixture.state)
        for change in ("tags", "tenant", "same-ids", "deployment", "name", "location"):
            with self.subTest(change=change):
                self.fixture.state = copy.deepcopy(original)
                row = self.fixture.row("identity")
                if change == "tags":
                    row["tags"]["managedBy"] = "azd-bicep"
                elif change == "tenant":
                    row["properties"]["tenantId"] = SUB
                elif change == "same-ids":
                    row["properties"]["clientId"] = PRINCIPAL
                elif change == "deployment":
                    row["properties"]["clientId"] = DEPLOY_CLIENT
                elif change == "name":
                    row["name"] += "-collision"
                else:
                    row["location"] = "westus"
                result, calls = self.fixture.run()
                self.assert_blocked(result, calls)
        self.fixture.state = original
        result, calls = self.fixture.run("--identity-resource-id", self.fixture.target.identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_mutations(calls)

    def test_role_permission_and_assignable_scope_collisions_fail_closed(self) -> None:
        self.fixture.complete()
        original = copy.deepcopy(self.fixture.state)
        for change in ("action", "data", "notactions", "scope", "type", "second-permission", "name-id"):
            with self.subTest(change=change):
                self.fixture.state = copy.deepcopy(original)
                row = self.fixture.row("metadata_role")
                props = row["properties"]
                if change == "action":
                    props["permissions"][0]["actions"] = ["Microsoft.CognitiveServices/*"]
                elif change == "data":
                    props["permissions"][0]["dataActions"] = ["*"]
                elif change == "notactions":
                    props["permissions"][0]["notActions"] = ["Microsoft.CognitiveServices/accounts/delete"]
                elif change == "scope":
                    props["assignableScopes"] = ["/"]
                elif change == "type":
                    props["type"] = "BuiltInRole"
                elif change == "second-permission":
                    props["permissions"].append(props["permissions"][0].copy())
                else:
                    row["id"] = row["id"].rsplit("/", 1)[0] + "/" + TENANT
                result, calls = self.fixture.run()
                self.assert_blocked(result, calls)

    def test_extra_inherited_group_and_descendant_grants_refuse_readiness(self) -> None:
        self.fixture.complete()
        self.fixture.configure()
        original = copy.deepcopy(self.fixture.state)
        for scope, principal in (
            ("/subscriptions/" + SUB, PRINCIPAL),
            ("/providers/Microsoft.Management/managementGroups/parent", PRINCIPAL),
            (self.fixture.target.group + "/providers/Microsoft.Storage/storageAccounts/other", PRINCIPAL),
            ("/subscriptions/" + SUB, GROUP_PRINCIPAL),
        ):
            with self.subTest(scope=scope, principal=principal):
                self.fixture.state = copy.deepcopy(original)
                row = copy.deepcopy(self.fixture.row("group_reader"))
                row["id"] = scope + "/providers/Microsoft.Authorization/roleAssignments/" + TENANT
                row["name"] = TENANT
                row["properties"]["scope"] = scope
                row["properties"]["principalId"] = principal
                self.fixture.state["resources"][row["id"]] = row
                self.fixture.state["groups"] = [GROUP_PRINCIPAL]
                result, calls = self.fixture.run("--verify-configuration")
                self.assert_blocked(result, calls, "Extra/inherited/group")
        self.fixture.state = original
        result, calls = self.fixture.run("--verify-configuration")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_mutations(calls)
        self.assertIn("--body 'true'", json.loads(result.stdout)["activation_command"])

    def test_client_id_assignment_is_rejected_on_readback(self) -> None:
        self.fixture.state["assign_client"] = True
        plan = self.fixture.plan()
        result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"])
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("Assignment scope/principal/role mismatch", result.stderr)
        self.assertEqual(len([c for c in calls if "PUT" in c]), 3)
        self.assertNotIn("--body 'true'", result.stdout)

    def test_wrong_federation_or_extra_credential_is_never_replaced(self) -> None:
        self.fixture.complete()
        original = copy.deepcopy(self.fixture.state)
        for change in ("issuer", "subject", "audiences", "expression", "extra"):
            with self.subTest(change=change):
                self.fixture.state = copy.deepcopy(original)
                row = self.fixture.row("federation")
                if change == "extra":
                    extra = copy.deepcopy(row)
                    extra["id"] += "-another"
                    self.fixture.state["resources"][extra["id"]] = extra
                elif change == "expression":
                    row["properties"]["claimsMatchingExpression"] = {"languageVersion": 1, "value": "*"}
                else:
                    row["properties"][change] = ["wrong"] if change == "audiences" else "wrong"
                result, calls = self.fixture.run()
                self.assert_blocked(result, calls)

    def test_partial_failures_preserve_state_and_replan_only_missing_steps(self) -> None:
        for fail_after in (False, True):
            with self.subTest(fail_after=fail_after):
                self.fixture.state["resources"] = {}
                self.fixture.state["writes"] = 0
                flag = "fail_after_write" if fail_after else "fail_write"
                self.fixture.state[flag] = 3
                plan = self.fixture.plan()
                result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"])
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(len([c for c in calls if "PUT" in c]), 3)
                self.assertNotIn("PRIVATE-", result.stderr + result.stdout)
                self.assertNotIn("DELETE", str(calls))
                self.assertIsNone(json.loads(result.stderr)["activation_command"])
                self.fixture.state.pop(flag)
                resumed = self.fixture.plan()
                self.assertEqual(sum(s["state"] == "verified" for s in resumed["steps"]), 3 if fail_after else 2)
                result, calls = self.fixture.run("--apply", "--approve-plan", resumed["plan_sha256"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len([c for c in calls if "PUT" in c]), 2 if fail_after else 3)

    def test_success_response_without_durable_readback_is_not_success(self) -> None:
        self.fixture.state["drop_write"] = 1
        plan = self.fixture.plan()
        result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"])
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("readback missing", result.stderr)
        self.assertEqual(len([c for c in calls if "PUT" in c]), 1)

    def test_activation_requires_complete_matching_configuration_and_accounts(self) -> None:
        result, calls = self.fixture.run("--verify-configuration")
        self.assert_blocked(result, calls, "incomplete")
        self.fixture.complete()
        result, calls = self.fixture.run("--verify-configuration")
        self.assert_blocked(result, calls, "configuration is incomplete")
        self.fixture.configure()
        original = copy.deepcopy(self.fixture.state)
        for change in ("missing-account", "duplicate-account", "account-tag", "active", "missing-deploy", "wrong-reader"):
            with self.subTest(change=change):
                self.fixture.state = copy.deepcopy(original)
                if change == "missing-account":
                    self.fixture.state["accounts"].pop()
                elif change == "duplicate-account":
                    self.fixture.state["accounts"].append(self.fixture.state["accounts"][0].copy())
                elif change == "account-tag":
                    self.fixture.state["accounts"][0]["tags"]["env"] = "another"
                elif change == "active":
                    self.fixture.state["variables"]["AI4IA_MODEL_RETIREMENT_REPORT_ENABLED"] = "true"
                elif change == "missing-deploy":
                    self.fixture.state["variables"].pop("AZURE_CLIENT_ID")
                else:
                    self.fixture.state["variables"]["AI4IA_MODEL_RETIREMENT_CLIENT_ID"] = PRINCIPAL
                result, calls = self.fixture.run("--verify-configuration")
                self.assert_blocked(result, calls)
        self.fixture.state = original
        result, calls = self.fixture.run("--verify-configuration")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_mutations(calls)
        data = json.loads(result.stdout)
        self.assertEqual(data["activation_command"],
                         "gh variable set 'AI4IA_MODEL_RETIREMENT_REPORT_ENABLED' --repo 'ian-t-adams/AI4IA' --body 'true'")

    def test_variable_pagination_and_selected_configuration_bind_plan(self) -> None:
        self.fixture.state["variables"].update({f"UNRELATED_{n}": "do-not-publish" for n in range(130)})
        result, calls = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("page=2" in c[-1] for c in calls if c[0] == "gh"))
        self.assertNotIn("do-not-publish", result.stdout)
        plan = json.loads(result.stdout)
        self.fixture.state["variables"]["AZURE_CLIENT_ID"] = TENANT
        result, calls = self.fixture.run("--apply", "--approve-plan", plan["plan_sha256"])
        self.assert_blocked(result, calls, "Stale/wrong")


class ReaderSetupSourceContracts(unittest.TestCase):
    def test_existing_quality_job_runs_behavioral_cli_tests(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "quality.yml").read_text())
        steps = workflow["jobs"]["script-tests"]["steps"]
        self.assertTrue(any("scripts.tests.test_retirement_reader_setup" in step.get("run", "") for step in steps))

    def test_setup_does_not_copy_a_profile_allowlist_or_add_a_runtime_gate(self) -> None:
        text = (ROOT / "scripts" / "setup-retirement-reader.py").read_text()
        self.assertNotIn('"maximum"', text)
        self.assertNotIn('"production"', text)
        workflow = yaml.safe_load((ROOT / setup.WORKFLOW).read_text())
        self.assertNotIn("environment", workflow["jobs"]["report"])
        env = workflow["jobs"]["activation"]["steps"][0]["env"]
        self.assertEqual(set(env), setup.VARIABLE_KEYS)

    def test_ids_are_stable_and_bound_to_explicit_scope(self) -> None:
        first = setup.Target(SUB, TENANT, "rg-demo-example", "example", "demo", REPO,
                             "baseline", "false", None)
        second = setup.Target(TENANT, SUB, "rg-demo-example", "example", "demo", REPO,
                              "baseline", "false", None)
        self.assertEqual(first.identity, copy.copy(first).identity)
        self.assertNotEqual(first.role_id.rsplit("/", 1)[1], second.role_id.rsplit("/", 1)[1])
        self.assertNotEqual(first.identity.rsplit("/", 1)[1], second.identity.rsplit("/", 1)[1])


if __name__ == "__main__":
    unittest.main()
