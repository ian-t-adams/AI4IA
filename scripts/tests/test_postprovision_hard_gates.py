"""Executable tests for postprovision model and topology hard gates."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from unittest.mock import patch

from scripts._external_model_deployments import load_external_model_deployments
from scripts.tests._postprovision import _ps_literal, _run_pwsh

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"
EXTERNAL_MANIFEST = REPO / "scripts" / "external-model-deployments.json"


def _run_model(
    *,
    overrides: dict[str, str | None] | None = None,
    token: str | None = "fake-management-token",
    deployments: list[dict[str, object]] | None = None,
    inventory: dict | None = None,
    fail_inventory: bool = False,
    external_manifest_path: str | Path | None = None,
) -> dict[str, object]:
    values: dict[str, str | None] = {
        "AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps(
            [
                {
                    "region": "swedencentral",
                    "accountName": "mf-example-sweden",
                    "deploymentNames": ["deployment-from-arm"],
                }
            ]
        ),
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
        "AZURE_RESOURCE_GROUP": "rg-ai4ia-test",
    }
    values.update(overrides or {})
    env_cases = "\n".join(
        f"    {_ps_literal(name)} {{ return {('$null' if value is None else _ps_literal(value))} }}"
        for name, value in values.items()
    )
    token_literal = "$null" if token is None else _ps_literal(token)
    response = json.dumps(
        inventory if inventory is not None else {
            "value": deployments
            if deployments is not None
            else [
                {
                    "name": "deployment-from-arm",
                    "properties": {"provisioningState": "Succeeded"},
                }
            ]
        }
    )
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$script:ExternalModelDeploymentsPath = {_ps_literal(str(external_manifest_path or EXTERNAL_MANIFEST))}
$fn = $ast.Find({{
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Test-ModelDeployment'
}}, $true)
if (-not $fn) {{ throw 'function not found' }}
Invoke-Expression $fn.Extent.Text
$script:Results = @()
$script:Captured = $null
function Add-Result {{
  param([string]$Name, [string]$Status, [string]$Detail)
  $script:Results += [pscustomobject]@{{ name=$Name; status=$Status; detail=$Detail }}
}}
function Get-EnvValue {{
  param([Parameter(Mandatory)][string]$Name)
  switch ($Name) {{
{env_cases}
    default {{ return $null }}
  }}
}}
function Get-MgmtToken {{ return {token_literal} }}
function Invoke-RestMethod {{
  param($Method, $Uri, $Headers, $TimeoutSec)
  {("throw 'synthetic inventory read failure'" if fail_inventory else "")}
  $script:Captured = [pscustomobject]@{{
    method = "$Method"
    uri = "$Uri"
    headers = $Headers
  }}
  return ({_ps_literal(response)} | ConvertFrom-Json)
}}
Test-ModelDeployment
[pscustomobject]@{{ results=$script:Results; captured=$script:Captured }} |
  ConvertTo-Json -Depth 8 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


def _run_model_with_external(
    *,
    target: dict[str, object],
    external_declarations: list[dict[str, str]],
    deployments: list[dict[str, object]],
) -> dict[str, object]:
    account_id = str(target["accountId"])
    parts = account_id.split("/")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "external-model-deployments.json"
        path.write_text(
            json.dumps({"schemaVersion": 1, "deployments": external_declarations}),
            encoding="utf-8",
        )
        return _run_model(
            overrides={
                "AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps([target]),
                "AZURE_SUBSCRIPTION_ID": parts[2],
                "AZURE_RESOURCE_GROUP": parts[4],
            },
            deployments=deployments,
            external_manifest_path=path,
        )


def _run_topology(overrides: dict[str, str | None] | None = None) -> dict[str, object]:
    values: dict[str, str | None] = {
        "AZURE_PROXY_URL": "https://proxy.example.test",
        "AZURE_MODEL_GATEWAY_URL": "https://proxy.example.test/openai",
        "AZURE_APIM_GATEWAY_URL": "https://apim.example.test",
        "AZURE_REALTIME_GATEWAY_URL": "https://apim.example.test/openai",
    }
    values.update(overrides or {})
    env_cases = "\n".join(
        f"    {_ps_literal(name)} {{ return {('$null' if value is None else _ps_literal(value))} }}"
        for name, value in values.items()
    )
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$fn = $ast.Find({{
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Test-GatewayTopology'
}}, $true)
if (-not $fn) {{ throw 'function not found' }}
Invoke-Expression $fn.Extent.Text
$script:Results = @()
function Add-Result {{
  param([string]$Name, [string]$Status, [string]$Detail)
  $script:Results += [pscustomobject]@{{ name=$Name; status=$Status; detail=$Detail }}
}}
function Get-EnvValue {{
  param([Parameter(Mandatory)][string]$Name)
  switch ($Name) {{
{env_cases}
    default {{ return $null }}
  }}
}}
Test-GatewayTopology
[pscustomobject]@{{ results=$script:Results }} | ConvertTo-Json -Depth 8 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


def _run_token_helper(name: str) -> dict[str, object]:
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$fn = $ast.Find({{
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq {_ps_literal(name)}
}}, $true)
if (-not $fn) {{ throw 'function not found' }}
Invoke-Expression $fn.Extent.Text
$script:AzdArgs = @()
$script:NativeCalls = @()
function azd {{
  $script:AzdArgs = [string[]]@($args)
  $global:LASTEXITCODE = 0
  return 'fake-azd-token'
}}
function az {{ throw 'Azure CLI fallback must not run when azd returns a token' }}
function Get-MonotonicTime {{ return 0 }}
function Invoke-NativeWithTimeout {{
  param($Command, $Arguments, $TimeoutSec)
  $script:NativeCalls += [pscustomobject]@{{
    command = $Command
    arguments = [string[]]@($Arguments)
    timeoutSec = $TimeoutSec
  }}
  return [pscustomobject]@{{ ExitCode = 0; Output = 'fake-azd-token'; TimedOut = $false }}
}}
$value = if ({_ps_literal(name)} -eq 'Get-CognitiveServicesToken') {{
  & {_ps_literal(name)} -TimeoutSec 60
}} else {{
  & {_ps_literal(name)}
}}
[pscustomobject]@{{ token=$value; azdArgs=$script:AzdArgs; nativeCalls=$script:NativeCalls }} |
  ConvertTo-Json -Depth 5 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


def _run_claude_dispatch(*, enabled: str, python_exit: int = 0) -> dict[str, object]:
    source = SCRIPT.read_text(encoding="utf-8")
    dispatch = source[source.index("$checks = @("):source.index("$pass = @(")]
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
# Load only actual script-scope declarations. Recursive AST extraction would
# wrongly promote a nested check to global scope and conceal dispatcher failure.
foreach ($statement in $ast.EndBlock.Statements) {{
  if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {{
    # Invoke-Expression loses the original file's automatic PSScriptRoot.
    # Bind only that path literal; preserve the function's original scope.
    Invoke-Expression $statement.Extent.Text.Replace(
      '$PSScriptRoot', {_ps_literal(_ps_literal(str(SCRIPT.parent)))}
    )
  }}
}}
$script:Results = [System.Collections.Generic.List[object]]::new()
$script:PythonCalls = @()
$script:Executed = @()
$ErrorActionPreference = 'Stop'
function Get-EnvValue {{
  param([Parameter(Mandatory)][string]$Name)
  switch ($Name) {{
    'AI4IA_CLAUDE_EXTERNAL_ENABLED' {{ return {_ps_literal(enabled)} }}
    'AZURE_PROXY_URL' {{ return 'https://proxy.example.test' }}
    'AZURE_MODEL_GATEWAY_URL' {{ return 'https://proxy.example.test/openai' }}
    'AZURE_APIM_GATEWAY_URL' {{ return 'https://apim.example.test' }}
    'AZURE_REALTIME_GATEWAY_URL' {{ return 'https://apim.example.test/openai' }}
    default {{ return $null }}
  }}
}}
function python {{
  $script:PythonCalls += ,([string[]]@($args))
  $global:LASTEXITCODE = {python_exit}
}}
function Test-ModelDeployment {{ $script:Executed += 'models' }}
function Test-ApiHealth {{ $script:Executed += 'api' }}
function Test-CustomDomainDns {{ $script:Executed += 'dns' }}
function Register-AppConfigurationSentinel {{ $script:Executed += 'sentinel' }}
function Register-ContentUnderstandingDefault {{ $script:Executed += 'content' }}
function az {{ throw 'Azure CLI must not execute in an offline dispatcher test' }}
function azd {{ throw 'azd must not execute in an offline dispatcher test' }}
{dispatch}
[pscustomobject]@{{
  results=@($script:Results); pythonCalls=$script:PythonCalls
  executed=$script:Executed
  exitCode=[int](@($script:Results | Where-Object {{ $_.Status -eq 'FAIL' }}).Count -gt 0)
}} | ConvertTo-Json -Depth 8 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


class ClaudeBindingDispatcherTests(unittest.TestCase):
    def test_default_off_dispatch_leaves_normal_topology_and_never_invokes_python(self):
        for disabled in ("", "false", "FALSE"):
            with self.subTest(disabled=disabled):
                payload = _run_claude_dispatch(enabled=disabled)
                self.assertEqual(payload["exitCode"], 0, payload["results"])
                self.assertEqual(payload["pythonCalls"], [])
                self.assertEqual([row["Name"] for row in payload["results"]], ["gateway-topology"])
                self.assertEqual(payload["executed"], ["models", "api", "dns", "sentinel", "content"])

    def test_staged_dispatch_passes_or_fails_from_actual_routed_python_result(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                payload = _run_claude_dispatch(enabled="true", python_exit=exit_code)
                self.assertEqual(payload["exitCode"], exit_code, payload["results"])
                self.assertEqual(payload["pythonCalls"], [[str(SCRIPT.parent / "check-claude-binding.py"), "--routed"]])
                self.assertEqual(
                    [(row["Name"], row["Status"]) for row in payload["results"]],
                    [("gateway-topology", "PASS"), ("claude-binding", "FAIL" if exit_code else "PASS")],
                )
                self.assertEqual(payload["executed"], ["models", "api", "dns", "sentinel", "content"])


class TokenAcquisitionTests(unittest.TestCase):
    def test_azd_raw_token_output_is_used_for_management(self) -> None:
        payload = _run_token_helper("Get-MgmtToken")
        self.assertEqual(payload["token"], "fake-azd-token")
        self.assertEqual(len(payload["nativeCalls"]), 1)
        self.assertEqual(payload["nativeCalls"][0]["command"], "azd")
        self.assertEqual(
            payload["nativeCalls"][0]["arguments"],
            ["auth", "token", "--scope", "https://management.azure.com/.default"],
        )
        self.assertEqual(payload["nativeCalls"][0]["timeoutSec"], 60)

    def test_azd_raw_token_output_is_used_for_cognitive_services(self) -> None:
        payload = _run_token_helper("Get-CognitiveServicesToken")
        self.assertEqual(payload["token"], "fake-azd-token")
        self.assertEqual(len(payload["nativeCalls"]), 1)
        self.assertEqual(payload["nativeCalls"][0]["command"], "azd")
        self.assertEqual(
            payload["nativeCalls"][0]["arguments"],
            [
                "auth",
                "token",
                "--scope",
                "https://cognitiveservices.azure.com/.default",
            ],
        )
        self.assertEqual(payload["nativeCalls"][0]["timeoutSec"], 60)


class ModelDeploymentHardGateTests(unittest.TestCase):
    def test_versioned_account_name_requires_nonblank_string(self):
        target, actual = self.evidence_fixture()
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")
        for value in ([target["accountName"]], 12345, None, "", "   ", True):
            with self.subTest(value=value):
                bad_target, bad_actual = deepcopy(target), deepcopy(actual)
                bad_target["accountName"] = value
                if type(value) is int:
                    bad_target["accountId"] = target["accountId"].replace(target["accountName"], str(value))
                    bad_actual["id"] = actual["id"].replace(target["accountName"], str(value))
                payload = self.run_evidence(bad_target, [bad_actual])
                self.assertEqual(payload["results"][0]["status"], "FAIL", payload)
                self.assertIsNone(payload["captured"])

    def test_json_integer_representations_and_invalid_types(self):
        for required in (True, False):
            target, actual = self.evidence_fixture()
            target["deploymentEvidence"][0]["required"] = required
            if not required:
                target["deploymentNames"] = []
            for capacity in (1, 10, 40, 2147483648):
                with self.subTest(required=required, capacity=capacity):
                    target["deploymentEvidence"][0]["capacity"] = capacity
                    actual["sku"]["capacity"] = capacity
                    self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")
            target["deploymentEvidence"][0]["capacity"] = 1
            actual["sku"]["capacity"] = 1
            for field in ("version", "expected", "actual"):
                for value in (True, False, 1.0, "1", None, 0, -1):
                    with self.subTest(required=required, field=field, value=value):
                        bad_target, bad_actual = deepcopy(target), deepcopy(actual)
                        if field == "version":
                            bad_target["verificationVersion"] = value
                        elif field == "expected":
                            bad_target["deploymentEvidence"][0]["capacity"] = value
                        else:
                            bad_actual["sku"]["capacity"] = value
                        self.assertEqual(
                            self.run_evidence(bad_target, [bad_actual])["results"][0]["status"], "FAIL",
                        )

    def evidence_fixture(self):
        account_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-ai4ia-test/providers/Microsoft.CognitiveServices/accounts/mf-example-sweden"
        evidence = {
            "name": "deployment-from-arm", "required": True,
            "model": {"format": "OpenAI", "name": "synthetic-model", "version": "1"},
            "sku": "GlobalStandard", "capacity": 10, "versionUpgradeOption": "NoAutoUpgrade",
        }
        target = {
            "region": "swedencentral", "accountName": "mf-example-sweden",
            "deploymentNames": [evidence["name"]], "accountId": account_id,
            "verificationVersion": 1, "deploymentEvidence": [evidence],
        }
        actual = {
            "id": account_id + "/deployments/" + evidence["name"], "name": evidence["name"],
            "sku": {"name": evidence["sku"], "capacity": evidence["capacity"]},
            "properties": {
                "model": deepcopy(evidence["model"]), "provisioningState": "Succeeded",
                "versionUpgradeOption": evidence["versionUpgradeOption"],
            },
        }
        return target, actual

    def run_evidence(self, target, deployments=None, **kwargs):
        return _run_model(
            overrides={"AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps([target])},
            deployments=deployments, **kwargs,
        )

    def test_versioned_required_and_known_retained_inventory(self):
        target, actual = self.evidence_fixture()
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")
        self.assertEqual(self.run_evidence(target, [])["results"][0]["status"], "FAIL")
        optional = deepcopy(target["deploymentEvidence"][0])
        optional.update(name="staged-known", required=False)
        target["deploymentEvidence"].append(optional)
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")
        retained = deepcopy(actual)
        retained.update(name=optional["name"], id=target["accountId"] + "/deployments/" + optional["name"])
        self.assertEqual(self.run_evidence(target, [actual, retained])["results"][0]["status"], "PASS")
        optional["required"] = True  # Externally managed runtime-on rows remain required.
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "FAIL")
        self.assertEqual(self.run_evidence(target, [actual, retained])["results"][0]["status"], "PASS")
        optional["required"] = False
        optional["capacity"] = None
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")
        payload = self.run_evidence(target, [actual, retained])
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("unknown capacity", payload["results"][0]["detail"])

    def test_versioned_exact_evidence_mutations_fail_for_required_and_retained(self):
        for required in (True, False):
            target, actual = self.evidence_fixture()
            target["deploymentEvidence"][0]["required"] = required
            if not required:
                target["deploymentNames"] = []
            for path, replacement in (
                (("id",), actual["id"].replace("/accounts/", "/wrong/")),
                (("name",), "unknown-deployment"),
                (("sku", "name"), "Standard"),
                (("sku", "name"), [actual["sku"]["name"]]),
                (("sku", "capacity"), 11),
                (("sku", "capacity"), True),
                (("sku", "capacity"), 10.0),
                (("properties", "model", "format"), "Other"),
                (("properties", "model", "name"), "other-model"),
                (("properties", "model", "version"), "2"),
                (("properties", "versionUpgradeOption"), "OnceNewDefaultVersionAvailable"),
                (("properties", "provisioningState"), "Creating"),
                (("properties", "provisioningState"), ["Succeeded"]),
                (("properties", "versionUpgradeOption"), ["NoAutoUpgrade"]),
                (("properties", "model"), {}),
                (("properties",), None),
                (("id",), None),
                (("sku", "capacity"), None),
            ):
                with self.subTest(required=required, path=path, replacement=replacement):
                    bad = deepcopy(actual)
                    node = bad
                    for part in path[:-1]:
                        node = node[part]
                    node[path[-1]] = replacement
                    self.assertEqual(self.run_evidence(target, [bad])["results"][0]["status"], "FAIL")
            self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")

    def test_versioned_malformed_evidence_and_duplicate_inventory_fail(self):
        target, actual = self.evidence_fixture()
        for field, values in (
            ("capacity", (True, False, 0, -1, 10.0, "10", None)),
            ("required", (None, "false", 0)),
            ("name", (None, "", "with/slash")),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    bad = deepcopy(target)
                    bad["deploymentEvidence"][0][field] = value
                    self.assertEqual(self.run_evidence(bad, [actual])["results"][0]["status"], "FAIL")
        for field in ("accountId", "verificationVersion", "deploymentEvidence"):
            bad = deepcopy(target)
            del bad[field]
            self.assertEqual(self.run_evidence(bad, [actual])["results"][0]["status"], "FAIL")
        for field, value in (
            ("accountId", target["accountId"].replace("rg-ai4ia-test", "rg-other")),
            ("verificationVersion", True),
            ("verificationVersion", 2),
            ("deploymentEvidence", None),
            ("deploymentNames", ["unknown"]),
            ("deploymentNames", [actual["name"], actual["name"]]),
        ):
            bad = deepcopy(target)
            bad[field] = value
            self.assertEqual(self.run_evidence(bad, [actual])["results"][0]["status"], "FAIL")
        bad = deepcopy(target)
        bad["deploymentEvidence"].append(deepcopy(bad["deploymentEvidence"][0]))
        self.assertEqual(self.run_evidence(bad, [actual])["results"][0]["status"], "FAIL")
        self.assertEqual(self.run_evidence(target, [actual, deepcopy(actual)])["results"][0]["status"], "FAIL")
        duplicate_id = deepcopy(actual)
        duplicate_id["name"] = "known-retained"
        retained = deepcopy(target["deploymentEvidence"][0])
        retained.update(name=duplicate_id["name"], required=False)
        target["deploymentEvidence"].append(retained)
        self.assertEqual(self.run_evidence(target, [actual, duplicate_id])["results"][0]["status"], "FAIL")
        payload = _run_model(
            overrides={"AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps([target, target])}, deployments=[actual],
        )
        self.assertIn("FAIL", [row["status"] for row in payload["results"]])
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")

    def test_complete_inventory_is_required_even_when_every_known_row_is_optional(self):
        target, actual = self.evidence_fixture()
        target["deploymentEvidence"][0]["required"] = False
        target["deploymentNames"] = []
        for response in (
            {}, {"value": None}, {"value": {}},
            {"value": [], "nextLink": "https://example.invalid/next"},
            {"value": [], "nextLink": ""},
            {"value": [actual], "@odata.nextLink": "https://example.invalid/next"},
            {"value": [{"name": None}]},
        ):
            with self.subTest(response=response):
                self.assertEqual(self.run_evidence(target, inventory=response)["results"][0]["status"], "FAIL")
        self.assertEqual(self.run_evidence(target, [], fail_inventory=True)["results"][0]["status"], "FAIL")
        self.assertEqual(self.run_evidence(target, [])["results"][0]["status"], "PASS")
        self.assertEqual(self.run_evidence(target, [actual])["results"][0]["status"], "PASS")

    def test_legacy_inventory_still_rejects_duplicate_and_unknown_names(self):
        baseline = {"name": "deployment-from-arm", "properties": {"provisioningState": "Succeeded"}}
        self.assertEqual(_run_model(deployments=[baseline])["results"][0]["status"], "PASS")
        for rows in ([baseline, deepcopy(baseline)], [baseline, {**baseline, "name": "unknown"}]):
            self.assertEqual(_run_model(deployments=rows)["results"][0]["status"], "FAIL")
        target = {
            "accountName": "mf-example-sweden",
            "deploymentNames": [baseline["name"], baseline["name"].upper()],
        }
        self.assertEqual(self.run_evidence(target, [baseline])["results"][0]["status"], "FAIL")

    def test_control_reaches_arm_with_bearer_token_and_passes(self) -> None:
        payload = _run_model()
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(payload["captured"]["method"], "Get")
        self.assertIn("/accounts/mf-example-sweden/deployments", payload["captured"]["uri"])
        self.assertEqual(
            payload["captured"]["headers"]["Authorization"],
            "Bearer fake-management-token",
        )

    def test_missing_expected_deployment_output_fails_closed(self) -> None:
        payload = _run_model(overrides={"AZURE_EXPECTED_MODEL_DEPLOYMENTS": None})
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("AZURE_EXPECTED_MODEL_DEPLOYMENTS", payload["results"][0]["detail"])
        self.assertIsNone(payload["captured"])

    def test_missing_subscription_or_resource_group_fails_closed(self) -> None:
        for name in ("AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP"):
            with self.subTest(name=name):
                payload = _run_model(overrides={name: None})
                self.assertEqual(payload["results"][0]["status"], "FAIL")
                self.assertIsNone(payload["captured"])

    def test_missing_management_token_fails_closed(self) -> None:
        payload = _run_model(token=None)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("token", payload["results"][0]["detail"])
        self.assertIsNone(payload["captured"])

    def test_zero_deployments_fails_closed_after_reaching_arm(self) -> None:
        payload = _run_model(deployments=[])
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("missing expected deployment", payload["results"][0]["detail"])
        self.assertIsNotNone(payload["captured"])

    def test_partial_deployment_set_fails_closed(self) -> None:
        payload = _run_model(
            overrides={
                "AZURE_EXPECTED_MODEL_DEPLOYMENTS": json.dumps(
                    [
                        {
                            "region": "swedencentral",
                            "accountName": "mf-example-sweden",
                            "deploymentNames": [
                                "deployment-from-arm",
                                "second-required-deployment",
                            ],
                        }
                    ]
                )
            }
        )
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("second-required-deployment", payload["results"][0]["detail"])

    def test_unexpected_stale_deployment_fails_exact_set_gate(self) -> None:
        payload = _run_model(
            deployments=[
                {
                    "name": "deployment-from-arm",
                    "properties": {"provisioningState": "Succeeded"},
                },
                {
                    "name": "removed-from-catalog",
                    "properties": {"provisioningState": "Succeeded"},
                },
            ]
        )
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("unexpected stale deployment", payload["results"][0]["detail"])
        self.assertIn("removed-from-catalog", payload["results"][0]["detail"])

    def test_exact_fleet_owned_deployment_is_reported_without_catalog_allocation(self) -> None:
        target, catalog_actual = self.evidence_fixture()
        external = load_external_model_deployments()[0]
        account_id = external["accountId"]
        parts = account_id.split("/")
        target.update(
            accountName=parts[-1],
            accountId=account_id,
        )
        catalog_actual["id"] = f"{account_id}/deployments/{catalog_actual['name']}"
        external_actual = {
            "id": f"{account_id}/deployments/{external['deploymentName']}",
            "name": external["deploymentName"],
            "sku": {"name": external["sku"], "capacity": 2},
            "properties": {
                "model": {
                    "format": external["modelFormat"],
                    "name": external["modelName"],
                    "version": external["modelVersion"],
                },
                "provisioningState": "Succeeded",
            },
        }
        payload = _run_model_with_external(
            target=target,
            external_declarations=[external],
            deployments=[catalog_actual, external_actual],
        )
        by_name = {row["name"]: row for row in payload["results"]}
        self.assertEqual(by_name[f"model-deployments/{parts[-1]}"]["status"], "PASS")
        self.assertIn("1 catalog deployment", by_name[f"model-deployments/{parts[-1]}"]["detail"])
        self.assertEqual(
            by_name[f"model-deployments/{parts[-1]}/external/{external['deploymentName']}"]["status"],
            "INFO",
        )

    def test_declared_fleet_owned_identity_mismatch_or_absence_fails(self) -> None:
        target, catalog_actual = self.evidence_fixture()
        external = load_external_model_deployments()[0]
        account_id = external["accountId"]
        parts = account_id.split("/")
        target.update(accountName=parts[-1], accountId=account_id)
        catalog_actual["id"] = f"{account_id}/deployments/{catalog_actual['name']}"
        expected = {
            "id": f"{account_id}/deployments/{external['deploymentName']}",
            "name": external["deploymentName"],
            "sku": {"name": external["sku"], "capacity": 2},
            "properties": {
                "model": {
                    "format": external["modelFormat"],
                    "name": external["modelName"],
                    "version": external["modelVersion"],
                },
                "provisioningState": "Succeeded",
            },
        }
        for field in ("format", "name", "version"):
            bad = deepcopy(expected)
            bad["properties"]["model"][field] = "mismatch"
            with self.subTest(field=field):
                payload = _run_model_with_external(
                    target=target,
                    external_declarations=[external],
                    deployments=[catalog_actual, bad],
                )
                self.assertEqual(payload["results"][0]["status"], "FAIL")
        bad_id = deepcopy(expected)
        bad_id["id"] = bad_id["id"].replace("/accounts/", "/wrong/")
        payload = _run_model_with_external(
            target=target,
            external_declarations=[external],
            deployments=[catalog_actual, bad_id],
        )
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        bad_sku = deepcopy(expected)
        bad_sku["sku"]["name"] = "Standard"
        payload = _run_model_with_external(
            target=target,
            external_declarations=[external],
            deployments=[catalog_actual, bad_sku],
        )
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        missing = _run_model_with_external(
            target=target,
            external_declarations=[external],
            deployments=[catalog_actual],
        )
        self.assertEqual(missing["results"][0]["status"], "FAIL")
        self.assertIn(external["deploymentName"], missing["results"][0]["detail"])

    def test_declared_account_must_be_inventoried_only_in_its_exact_scope(self) -> None:
        parts = load_external_model_deployments()[0]["accountId"].split("/")
        for subscription, resource_group, must_fail in (
            (parts[2], parts[4], True),
            (parts[2].upper(), parts[4].upper(), True),
            ("00000000-0000-0000-0000-000000000000", parts[4], False),
            (parts[2], parts[4] + "-other", False),
        ):
            with self.subTest(subscription=subscription, resource_group=resource_group):
                payload = _run_model(overrides={
                    "AZURE_SUBSCRIPTION_ID": subscription,
                    "AZURE_RESOURCE_GROUP": resource_group,
                })
                self.assertIn("/accounts/mf-example-sweden/deployments?", payload["captured"]["uri"])
                failures = [row for row in payload["results"] if row["status"] == "FAIL"]
                if must_fail:
                    self.assertTrue(failures)
                    self.assertIn("declared external account was not completely inventoried", failures[0]["detail"])
                else:
                    self.assertFalse(failures)
                    self.assertEqual(payload["results"][0]["status"], "PASS")

    def test_external_manifest_rejects_wildcards_and_prefixes_at_load(self) -> None:
        template = deepcopy(load_external_model_deployments()[0])
        for name in ("fal-*", "fal-ltx-"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                template["deploymentName"] = name
                path = Path(directory) / "external-model-deployments.json"
                path.write_text(
                    json.dumps({"schemaVersion": 1, "deployments": [template]}),
                    encoding="utf-8",
                )
                payload = _run_model(
                    external_manifest_path=path,
                )
                self.assertEqual(payload["results"][0]["status"], "FAIL")
                self.assertIn("exact literal identities", payload["results"][0]["detail"])
                self.assertIsNone(payload["captured"])


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 requires Windows")
class WindowsPowerShell51ModelDeploymentTests(ModelDeploymentHardGateTests):
    def setUp(self):
        executable = (
            Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell"
            / "v1.0" / "powershell.exe"
        )
        if not executable.is_file():
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        runner = partial(_run_pwsh, executable=str(executable))
        runner_patch = patch(__name__ + "._run_pwsh", runner)
        self.addCleanup(runner_patch.stop)
        runner_patch.start()


class GatewayTopologyHardGateTests(unittest.TestCase):
    def test_valid_split_topology_passes(self) -> None:
        payload = _run_topology()
        self.assertEqual(payload["results"][0]["status"], "PASS")

    def test_each_missing_topology_output_fails_closed(self) -> None:
        for name in (
            "AZURE_PROXY_URL",
            "AZURE_MODEL_GATEWAY_URL",
            "AZURE_APIM_GATEWAY_URL",
            "AZURE_REALTIME_GATEWAY_URL",
        ):
            with self.subTest(name=name):
                payload = _run_topology({name: None})
                self.assertEqual(payload["results"][0]["status"], "FAIL")
                self.assertIn(name, payload["results"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
