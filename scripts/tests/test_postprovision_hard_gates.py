"""Executable tests for postprovision model and topology hard gates."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.tests._postprovision import _ps_literal, _run_pwsh

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"


def _run_model(
    *,
    overrides: dict[str, str | None] | None = None,
    token: str | None = "fake-management-token",
    deployments: list[dict[str, object]] | None = None,
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
        {
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
