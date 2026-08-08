"""Execute the postprovision Content Understanding setup with every edge stubbed.

The first implementation was lint-clean and reviewable, but called
``Get-EnvValue`` with two positional arguments even though the helper accepts
one. It also invented two azd variables that no Bicep output creates. The real
deploy failed in the postprovision hook *before any image was built*:

    A positional parameter cannot be found that accepts argument
    'AZURE_FOUNDRY_ACCOUNT_NAME'.

Static checks did not catch it. These tests use PowerShell's own parser to load
only the function under test, replace Azure/HTTP with process-local functions,
and execute the same parameter binding and catalog lookup CI executes.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"
CATALOG = REPO / "app" / "api" / "src" / "ai4ia_api" / "data" / "model_catalog.json"


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run(endpoints_json: str | None, *, patch_failures: int = 0) -> dict[str, object]:
    endpoints = "$null" if endpoints_json is None else _ps_literal(endpoints_json)
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
    $node.Name -eq 'Register-ContentUnderstandingDefault'
}}, $true)
if (-not $fn) {{ throw 'function not found' }}
Invoke-Expression $fn.Extent.Text

$script:Results = @()
$script:Captured = $null
$script:PatchCalls = 0
$script:FailuresRemaining = {patch_failures}
function Add-Result {{
  param([string]$Name, [string]$Status, [string]$Detail)
  $script:Results += [pscustomobject]@{{ name=$Name; status=$Status; detail=$Detail }}
}}
function Get-EnvValue {{
  param([Parameter(Mandatory)][string]$Name)
  if ($Name -eq 'AZURE_FOUNDRY_ENDPOINTS') {{ return {endpoints} }}
  return $null
}}
function az {{
  return 'fake-token'
}}
function Start-Sleep {{ param($Seconds) }}
function Invoke-RestMethod {{
  param($Method, $Uri, $Headers, $Body, $TimeoutSec)
  $script:PatchCalls++
  if ($script:FailuresRemaining -gt 0) {{
    $script:FailuresRemaining--
    throw 'RBAC has not propagated'
  }}
  $script:Captured = [pscustomobject]@{{
    method = "$Method"
    uri = "$Uri"
    body = ($Body | ConvertFrom-Json)
  }}
  return @{{}}
}}

Register-ContentUnderstandingDefault -CatalogPath {_ps_literal(str(CATALOG))}
[pscustomobject]@{{
  results = $script:Results
  captured = $script:Captured
  patchCalls = $script:PatchCalls
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


class ContentUnderstandingPostprovisionTests(unittest.TestCase):
    def test_uses_real_foundry_output_and_catalog_deployments(self) -> None:
        payload = _run(
            json.dumps(
                [
                    {
                        "region": "swedencentral",
                        "accountName": "mf-example-sweden",
                        "endpoint": "https://mf-example-sweden.cognitiveservices.azure.com/",
                    },
                    {
                        "region": "eastus2",
                        "accountName": "mf-example-eastus2",
                        "endpoint": "https://mf-example-eastus2.cognitiveservices.azure.com/",
                    },
                ]
            )
        )
        result = payload["results"][0]
        captured = payload["captured"]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(captured["method"], "Patch")
        self.assertEqual(
            captured["uri"],
            "https://mf-example-eastus2.cognitiveservices.azure.com/"
            "contentunderstanding/defaults?api-version=2025-11-01",
        )
        self.assertEqual(
            captured["body"]["modelDeployments"],
            {
                "prebuilt-analyzer-completion-mini": "gpt-5.2-slurmfactory-eastus2-glbl",
                "prebuilt-analyzer-completion": "gpt-5.2-slurmfactory-eastus2-glbl",
                "prebuilt-analyzer-embedding": "text-embedding-3-large-slurmfactory-eastus2-glbl",
            },
        )

    def test_missing_output_skips_without_throwing(self) -> None:
        payload = _run(None)
        self.assertIsNone(payload["captured"])
        self.assertEqual(payload["results"], [
            {
                "name": "Content Understanding defaults",
                "status": "SKIP",
                "detail": "AZURE_FOUNDRY_ENDPOINTS not set",
            }
        ])

    def test_missing_eastus2_account_warns_without_throwing(self) -> None:
        payload = _run(
            json.dumps(
                [{"region": "westus", "accountName": "mf-example-westus", "endpoint": "https://x"}]
            )
        )
        self.assertIsNone(payload["captured"])
        self.assertEqual(payload["results"][0]["status"], "WARN")
        self.assertIn("no eastus2 account", payload["results"][0]["detail"])

    def test_retries_through_role_assignment_propagation(self) -> None:
        payload = _run(
            json.dumps(
                [{"region": "eastus2", "accountName": "mf-example-eastus2", "endpoint": "https://e"}]
            ),
            patch_failures=2,
        )
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(payload["patchCalls"], 3)


class ContentUnderstandingProvisioningWiringTests(unittest.TestCase):
    def test_workflow_resolves_object_id_and_exports_it_for_azd(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("AZURE_PRINCIPAL_ID=$principal_id", workflow)
        self.assertIn("az identity list", workflow)
        self.assertIn(".principalId", workflow)
        self.assertIn("az ad sp show --id", workflow)

    def test_bicep_grants_only_the_narrow_role_on_the_primary_account(self) -> None:
        main = (REPO / "infra" / "main.bicep").read_text(encoding="utf-8")
        foundry = (REPO / "infra" / "modules" / "foundry.bicep").read_text(encoding="utf-8")
        params = (REPO / "infra" / "main.parameters.json").read_text(encoding="utf-8")
        self.assertIn("${AZURE_PRINCIPAL_ID=}", params)
        self.assertIn("deploymentPrincipalId string = ''", main)
        self.assertIn(
            "contentUnderstandingPrincipalIds: (i == primaryFoundryIndex) "
            "? contentUnderstandingPrincipalIds : []",
            main,
        )
        self.assertIn(
            "for pid in contentUnderstandingPrincipalIds",
            foundry,
        )
        self.assertNotIn(
            "for pid in dataPlanePrincipalIds: {\n"
            "  name: guid(account.id, pid, contentUnderstandingRoleId)",
            foundry,
        )


if __name__ == "__main__":
    unittest.main()
