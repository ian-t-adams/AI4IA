"""Execute Content Understanding postprovision reconciliation with every edge stubbed.

These tests use PowerShell's parser to load the real function, replace Azure and
HTTP with process-local functions, and prove the azd output contract without
network access or Azure mutation.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from scripts.tests._postprovision import _ps_literal, _run_pwsh

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"
GREENFIELD = REPO / "docs" / "runbooks" / "greenfield-standup.md"


def _run(
    *,
    overrides: dict[str, str | None] | None = None,
    patch_failures: int = 0,
    token: str | None = "fake-cognitive-token",
    request_seconds: int = 0,
    token_seconds: int = 0,
) -> dict[str, object]:
    values: dict[str, str | None] = {
        "AZURE_CONTENT_UNDERSTANDING_ENABLED": "true",
        "AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME": "mf-example-sweden",
        "AZURE_PRIMARY_FOUNDRY_REGION": "swedencentral",
        "AZURE_PRIMARY_FOUNDRY_ENDPOINT": "https://mf-example-sweden.cognitiveservices.azure.com/",
        # Deliberately do not resemble the naming convention. If the script ever
        # reconstructs names instead of consuming outputs, the assertion catches it.
        "AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT": "completion-from-bicep-output",
        "AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT": "embedding-from-bicep-output",
        "AZURE_CONTENT_UNDERSTANDING_PREVIEW_ENABLED": "true",
        "AZURE_CONTENT_UNDERSTANDING_COMPLETION_CAPACITY": "50",
        "AZURE_CONTENT_UNDERSTANDING_AGENTIC_ANALYZER_ID": "",
    }
    values.update(overrides or {})
    env_cases = "\n".join(
        f"    {_ps_literal(name)} {{ return {('$null' if value is None else _ps_literal(value))} }}"
        for name, value in values.items()
    )
    token_literal = "$null" if token is None else _ps_literal(token)
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
$script:GetCalls = 0
$script:TokenCalls = 0
$script:SleepSeconds = [System.Collections.Generic.List[int]]::new()
$script:NowSeconds = 0
$script:RequestSeconds = {request_seconds}
$script:TokenSeconds = {token_seconds}
$script:FailuresRemaining = {patch_failures}
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
$tokenValue = {token_literal}
function Get-CognitiveServicesToken {{
  param($TimeoutSec)
  $script:TokenCalls++
  $script:NowSeconds += [Math]::Min($script:TokenSeconds, $TimeoutSec)
  return $tokenValue
}}
function Start-Sleep {{
  param($Seconds)
  $script:SleepSeconds.Add([int]$Seconds)
  $script:NowSeconds += [int]$Seconds
}}
function Get-MonotonicTime {{ return [double]$script:NowSeconds }}
function Invoke-RestMethod {{
  param($Method, $Uri, $Headers, $Body, $TimeoutSec)
  if ("$Method" -eq 'Get') {{
    $script:GetCalls++
    if ($Uri -like '*prebuilt-documentSearch*') {{
      return @{{ supportedModels = @{{
        completion = @('gpt-5.2')
        embedding = @('text-embedding-3-large')
      }} }}
    }}
    if ($Uri -like '*agentic.contract*') {{
      return @{{ config = @{{ workflow = 'agentic.2026-06-01-preview' }} }}
    }}
    return @{{ config = @{{ workflow = 'standard.2026-06-01-preview' }} }}
  }}
  $script:PatchCalls++
  $script:NowSeconds += [Math]::Min($script:RequestSeconds, $TimeoutSec)
  if ($script:FailuresRemaining -gt 0) {{
    $script:FailuresRemaining--
    throw 'RBAC has not propagated'
  }}
  $script:Captured = [pscustomobject]@{{
    method = "$Method"
    uri = "$Uri"
    headers = $Headers
    body = ($Body | ConvertFrom-Json)
  }}
  return @{{}}
}}

Register-ContentUnderstandingDefault
[pscustomobject]@{{
  results = $script:Results
  captured = $script:Captured
  patchCalls = $script:PatchCalls
  getCalls = $script:GetCalls
  tokenCalls = $script:TokenCalls
  sleepSeconds = $script:SleepSeconds
  elapsedSeconds = $script:NowSeconds
}} | ConvertTo-Json -Depth 8 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


class ContentUnderstandingPostprovisionTests(unittest.TestCase):
    def test_non_eastus2_primary_uses_exact_bicep_outputs(self) -> None:
        payload = _run()
        result = payload["results"][0]
        captured = payload["captured"]
        self.assertEqual(result["status"], "PASS")
        self.assertIn("account=mf-example-sweden", result["detail"])
        self.assertIn("region=swedencentral", result["detail"])
        self.assertEqual(captured["method"], "Patch")
        self.assertEqual(
            captured["uri"],
            "https://mf-example-sweden.cognitiveservices.azure.com/"
            "contentunderstanding/defaults?api-version=2025-11-01",
        )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer fake-cognitive-token")
        self.assertEqual(
            captured["headers"]["Content-Type"], "application/merge-patch+json"
        )
        self.assertEqual(
            captured["body"]["modelDeployments"],
            {
                "prebuilt-analyzer-completion-mini": "completion-from-bicep-output",
                "prebuilt-analyzer-completion": "completion-from-bicep-output",
                "prebuilt-analyzer-embedding": "embedding-from-bicep-output",
                "gpt-5.2": "completion-from-bicep-output",
                "text-embedding-3-large": "embedding-from-bicep-output",
            },
        )
        self.assertEqual(payload["getCalls"], 8)

    def test_disabled_content_understanding_is_the_only_skip_path(self) -> None:
        payload = _run(
            overrides={
                "AZURE_CONTENT_UNDERSTANDING_ENABLED": "false",
                "AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME": None,
                "AZURE_PRIMARY_FOUNDRY_REGION": None,
                "AZURE_PRIMARY_FOUNDRY_ENDPOINT": None,
            }
        )
        self.assertEqual(payload["results"][0]["status"], "SKIP")
        self.assertEqual(payload["patchCalls"], 0)

    def test_missing_enabled_output_fails_closed(self) -> None:
        payload = _run(overrides={"AZURE_CONTENT_UNDERSTANDING_ENABLED": None})
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("AZURE_CONTENT_UNDERSTANDING_ENABLED", payload["results"][0]["detail"])

    def test_enabled_content_understanding_requires_every_primary_output(self) -> None:
        names = (
            "AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME",
            "AZURE_PRIMARY_FOUNDRY_REGION",
            "AZURE_PRIMARY_FOUNDRY_ENDPOINT",
            "AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT",
            "AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT",
            "AZURE_CONTENT_UNDERSTANDING_PREVIEW_ENABLED",
            "AZURE_CONTENT_UNDERSTANDING_COMPLETION_CAPACITY",
        )
        for name in names:
            with self.subTest(name=name):
                payload = _run(overrides={name: None})
                self.assertEqual(payload["results"][0]["status"], "FAIL")
                self.assertIn(name, payload["results"][0]["detail"])
                self.assertEqual(payload["patchCalls"], 0)

    def test_missing_cognitive_services_token_fails_closed(self) -> None:
        payload = _run(token=None)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("token", payload["results"][0]["detail"])
        self.assertEqual(payload["patchCalls"], 0)

    def test_ga_only_mode_checks_document_search_but_skips_preview_analyzers(self) -> None:
        payload = _run(
            overrides={"AZURE_CONTENT_UNDERSTANDING_PREVIEW_ENABLED": "false"}
        )
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(payload["getCalls"], 1)

    def test_agentic_configuration_requires_400k_completion_capacity(self) -> None:
        payload = _run(
            overrides={
                "AZURE_CONTENT_UNDERSTANDING_AGENTIC_ANALYZER_ID": "agentic.contract",
                "AZURE_CONTENT_UNDERSTANDING_COMPLETION_CAPACITY": "399",
            }
        )
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("400K TPM", payload["results"][0]["detail"])
        self.assertEqual(payload["patchCalls"], 0)

    def test_agentic_configuration_is_verified_when_capacity_is_sufficient(self) -> None:
        payload = _run(
            overrides={
                "AZURE_CONTENT_UNDERSTANDING_AGENTIC_ANALYZER_ID": "agentic.contract",
                "AZURE_CONTENT_UNDERSTANDING_COMPLETION_CAPACITY": "400",
            }
        )
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(payload["getCalls"], 9)

    def test_retries_through_role_assignment_propagation(self) -> None:
        payload = _run(patch_failures=2)
        self.assertEqual(payload["results"][0]["status"], "PASS")
        self.assertEqual(payload["patchCalls"], 3)
        self.assertEqual(payload["tokenCalls"], 3)
        self.assertEqual(payload["sleepSeconds"], [30, 30])

    def test_patch_exhaustion_is_a_hard_failure(self) -> None:
        payload = _run(patch_failures=99)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertIn("900-second budget", payload["results"][0]["detail"])
        self.assertEqual(payload["patchCalls"], 30)
        self.assertEqual(payload["tokenCalls"], 30)
        self.assertEqual(payload["sleepSeconds"], [30] * 30)
        self.assertEqual(payload["elapsedSeconds"], 900)

    def test_full_timeout_requests_never_exceed_wall_clock_budget(self) -> None:
        payload = _run(patch_failures=99, request_seconds=60)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertLessEqual(payload["elapsedSeconds"], 900)
        self.assertEqual(payload["elapsedSeconds"], 900)
        self.assertEqual(payload["patchCalls"], 10)
        self.assertEqual(payload["tokenCalls"], 10)

    def test_full_timeout_token_refreshes_never_exceed_wall_clock_budget(self) -> None:
        payload = _run(patch_failures=99, token_seconds=60)
        self.assertEqual(payload["results"][0]["status"], "FAIL")
        self.assertLessEqual(payload["elapsedSeconds"], 900)
        self.assertEqual(payload["elapsedSeconds"], 900)
        self.assertEqual(payload["tokenCalls"], 10)

    def test_function_does_not_read_endpoint_array_or_runtime_catalog(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        function = source.split("function Register-ContentUnderstandingDefault {", 1)[1].split(
            "$checks = @(", 1
        )[0]
        self.assertNotIn("AZURE_FOUNDRY_ENDPOINTS", function)
        self.assertNotIn("model_catalog.json", function)
        self.assertNotIn("eastus2", function)


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
        self.assertIn("for pid in contentUnderstandingPrincipalIds", foundry)
        self.assertNotIn(
            "for pid in dataPlanePrincipalIds: {\n"
            "  name: guid(account.id, pid, contentUnderstandingRoleId)",
            foundry,
        )

    def test_bicep_emits_explicit_primary_cu_outputs(self) -> None:
        main = (REPO / "infra" / "main.bicep").read_text(encoding="utf-8")
        for name in (
            "AZURE_CONTENT_UNDERSTANDING_ENABLED",
            "AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME",
            "AZURE_PRIMARY_FOUNDRY_REGION",
            "AZURE_PRIMARY_FOUNDRY_ENDPOINT",
            "AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT",
            "AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT",
            "AZURE_CONTENT_UNDERSTANDING_PREVIEW_ENABLED",
            "AZURE_CONTENT_UNDERSTANDING_AGENTIC_ANALYZER_ID",
            "AZURE_CONTENT_UNDERSTANDING_COMPLETION_CAPACITY",
        ):
            self.assertIn(f"output {name} ", main)
        self.assertIn("deployments: modelDeploymentsByRegion[i]", main)

    def test_greenfield_documents_cu_as_a_hard_provision_gate(self) -> None:
        runbook = GREENFIELD.read_text(encoding="utf-8")
        first_provision = runbook.split(
            "### 6.1 First provision without custom domains", 1
        )[1].split("### 6.2 Bind custom domains", 1)[0]
        self.assertIn("The first standup must use the GitHub deployment workflow", first_provision)
        self.assertIn("principal/object id", first_provision)
        self.assertIn("`AZURE_PRINCIPAL_ID`", first_provision)
        self.assertIn("never treating the\nclient id as an object id", first_provision)
        self.assertIn("Cognitive Services Content Understanding Contributor", first_provision)
        self.assertNotIn("\nazd up\n", first_provision)
        self.assertIn("Content Understanding defaults | PASS", runbook)
        self.assertIn("fails the provision", runbook)
        self.assertNotIn("postprovision reports a Content Understanding defaults `WARN`", runbook)


if __name__ == "__main__":
    unittest.main()
