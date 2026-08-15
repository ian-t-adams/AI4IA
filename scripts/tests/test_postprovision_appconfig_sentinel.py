"""Execute App Configuration sentinel reconciliation with Azure CLI stubbed."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from scripts.tests._postprovision import _ps_literal, _run_pwsh

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"


def _run(
    *,
    endpoint: str | None = "https://appcs-example.azconfig.io",
    label: str | None = None,
    failures: int = 0,
    principal_id: str | None = "deploy-principal",
    command_seconds: int = 0,
) -> dict[str, object]:
    values = {
        "AZURE_APP_CONFIG_ENDPOINT": endpoint,
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
$script:NowSeconds = 0
$script:CommandSeconds = {command_seconds}
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
  $script:NowSeconds += [int]$Seconds
}}
function Get-MonotonicTime {{ return [double]$script:NowSeconds }}
function Invoke-AppConfigSet {{
  param([string[]]$Arguments, [int]$TimeoutSec)
  $script:Calls.Add([string[]]@($Arguments))
  $script:NowSeconds += [Math]::Min($script:CommandSeconds, $TimeoutSec)
  if ($script:FailuresRemaining -gt 0) {{
    $script:FailuresRemaining--
    return 1
  }}
  return 0
}}

Register-AppConfigurationSentinel
[pscustomobject]@{{
  results = $script:Results
  calls = $script:Calls
  sleeps = $script:Sleeps
  sleepSeconds = $script:SleepSeconds
  elapsedSeconds = $script:NowSeconds
}} | ConvertTo-Json -Depth 8 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


def _run_timeout_helper(*, mode: str, exit_code: int = 0) -> tuple[int, float]:
    if shutil.which("pwsh") is None:
        raise unittest.SkipTest("pwsh is required for executable postprovision tests")
    with tempfile.TemporaryDirectory() as tmp:
        stub_dir = Path(tmp)
        if mode != "missing":
            if os.name == "nt":
                stub = stub_dir / "az.cmd"
                stub.write_text(
                    "@echo off\r\n"
                    'if "%AZ_STUB_MODE%"=="hang" pwsh -NoProfile -Command '
                    '"Start-Sleep -Seconds 5"\r\n'
                    "exit /b %AZ_STUB_EXIT%\r\n",
                    encoding="ascii",
                )
            else:
                stub = stub_dir / "az"
                stub.write_text(
                    '#!/usr/bin/env sh\n'
                    'if [ "$AZ_STUB_MODE" = "hang" ]; then sleep 5; fi\n'
                    'exit "$AZ_STUB_EXIT"\n',
                    encoding="ascii",
                )
                stub.chmod(0o755)

        command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$functions = $ast.FindAll({{
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -in @('Invoke-NativeWithTimeout', 'Invoke-AppConfigSet')
}}, $true)
if ($functions.Count -ne 2) {{ throw 'functions not found' }}
$functions | Sort-Object {{ if ($_.Name -eq 'Invoke-NativeWithTimeout') {{ 0 }} else {{ 1 }} }} |
  ForEach-Object {{ Invoke-Expression $_.Extent.Text }}
$result = Invoke-AppConfigSet -Arguments @('appconfig', 'kv', 'set') -TimeoutSec 1
Write-Output $result
"""
        env = dict(os.environ)
        env["AZ_STUB_MODE"] = mode
        env["AZ_STUB_EXIT"] = str(exit_code)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        started = time.monotonic()
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            raise AssertionError(
                f"PowerShell failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}"
            )
        return int(proc.stdout.strip().splitlines()[-1]), elapsed


class AppConfigurationTimeoutHelperTests(unittest.TestCase):
    def test_native_nonzero_exit_is_propagated(self) -> None:
        result, _ = _run_timeout_helper(mode="exit", exit_code=23)
        self.assertEqual(result, 23)

    def test_missing_azure_cli_is_not_success(self) -> None:
        result, _ = _run_timeout_helper(mode="missing")
        self.assertNotEqual(result, 0)

    def test_hung_azure_cli_is_terminated_at_timeout(self) -> None:
        result, elapsed = _run_timeout_helper(mode="hang")
        self.assertEqual(result, 124)
        self.assertLess(elapsed, 4)


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

    def test_missing_unconditional_endpoint_output_fails_closed(self) -> None:
        payload = _run(endpoint=None)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("AZURE_APP_CONFIG_ENDPOINT", payload["results"][0]["detail"])
        self.assertEqual(payload["calls"], [])
        self.assertEqual(payload["sleeps"], 0)

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
        self.assertIn("900-second budget", result["detail"])
        self.assertEqual(len(payload["calls"]), 30)
        self.assertEqual(payload["sleeps"], 30)
        self.assertEqual(payload["sleepSeconds"], [30] * 30)
        self.assertEqual(payload["elapsedSeconds"], 900)

    def test_full_timeout_commands_never_exceed_wall_clock_budget(self) -> None:
        payload = _run(failures=99, command_seconds=60)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertLessEqual(payload["elapsedSeconds"], 900)
        self.assertEqual(payload["elapsedSeconds"], 900)
        self.assertEqual(len(payload["calls"]), 10)


if __name__ == "__main__":
    unittest.main()
