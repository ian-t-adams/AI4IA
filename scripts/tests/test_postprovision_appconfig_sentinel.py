"""Execute App Configuration sentinel reconciliation with Azure CLI stubbed."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run(
    *,
    label: str | None = None,
    failures: int = 0,
    principal_id: str | None = "deploy-principal",
) -> dict[str, object]:
    if shutil.which("pwsh") is None:
        raise unittest.SkipTest("pwsh is required for executable postprovision tests")
    values = {
        "AZURE_APP_CONFIG_ENDPOINT": "https://appcs-example.azconfig.io",
        "AZURE_APP_CONFIG_LABEL": label,
        "AZURE_PRINCIPAL_ID": principal_id,
    }
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
    $node.Name -eq 'Register-AppConfigurationSentinel'
}}, $true)
if (-not $fn) {{ throw 'function not found' }}
Invoke-Expression $fn.Extent.Text

$script:Results = @()
$script:Calls = [System.Collections.Generic.List[object]]::new()
$script:Sleeps = 0
$script:SleepSeconds = [System.Collections.Generic.List[int]]::new()
$script:FailuresRemaining = {failures}
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
function Start-Sleep {{
  param($Seconds)
  $script:Sleeps++
  $script:SleepSeconds.Add([int]$Seconds)
}}
function az {{
  $script:Calls.Add([string[]]@($args))
  if ($script:FailuresRemaining -gt 0) {{
    $script:FailuresRemaining--
    $global:LASTEXITCODE = 1
    return
  }}
  $global:LASTEXITCODE = 0
}}

Register-AppConfigurationSentinel
[pscustomobject]@{{
  results = $script:Results
  calls = $script:Calls
  sleeps = $script:Sleeps
  sleepSeconds = $script:SleepSeconds
}} | ConvertTo-Json -Depth 8 -Compress
"""
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise AssertionError(f"PowerShell failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class AppConfigurationSentinelTests(unittest.TestCase):
    def _assert_keyless_login_call(self, call: list[str]) -> None:
        self.assertEqual(
            call[:3],
            ["appconfig", "kv", "set"],
        )
        self.assertIn("--auth-mode", call)
        self.assertEqual(call[call.index("--auth-mode") + 1], "login")
        self.assertIn("--endpoint", call)
        self.assertIn("--yes", call)
        self.assertIn("--output", call)
        self.assertEqual(call[call.index("--key") + 1], "Warm:Sentinel")
        self.assertEqual(call[call.index("--value") + 1], "ready")
        forbidden = {"key", "--connection-string", "--access-key", "--name"}
        self.assertTrue(forbidden.isdisjoint(call), call)

    def test_unlabeled_set_uses_entra_login(self) -> None:
        payload = _run()
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(len(payload["calls"]), 1)
        call = payload["calls"][0]
        self._assert_keyless_login_call(call)
        self.assertNotIn("--label", call)

    def test_local_provision_without_oidc_principal_leaves_existing_sentinel(self) -> None:
        payload = _run(principal_id=None)
        self.assertEqual(payload["results"][0]["status"], "SKIP")
        self.assertIn("AZURE_PRINCIPAL_ID not set", payload["results"][0]["detail"])
        self.assertEqual(payload["calls"], [])
        self.assertEqual(payload["sleeps"], 0)

    def test_label_is_passed_as_one_exact_argument(self) -> None:
        payload = _run(label="Production Blue")
        call = payload["calls"][0]
        self._assert_keyless_login_call(call)
        index = call.index("--label")
        self.assertEqual(call[index + 1], "Production Blue")

    def test_retries_role_propagation_then_passes(self) -> None:
        payload = _run(failures=2)
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(len(payload["calls"]), 3)
        self.assertEqual(payload["sleeps"], 2)

    def test_final_failure_fails_after_the_documented_rbac_window(self) -> None:
        payload = _run(failures=99)
        result = payload["results"][0]
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("failed after 31 attempts", result["detail"])
        self.assertEqual(len(payload["calls"]), 31)
        self.assertEqual(payload["sleeps"], 30)
        self.assertEqual(payload["sleepSeconds"], [30] * 30)


if __name__ == "__main__":
    unittest.main()
