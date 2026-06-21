<#
.SYNOPSIS
  Non-fatal follow-up reminder after `azd provision`.

.DESCRIPTION
  Runs after `azd provision` and prints the manual checks that remain outside
  Bicep/azd. Non-fatal by design (azure.yaml sets continueOnError: true).
#>
$ErrorActionPreference = "Continue"
Write-Host "== AI4IA postprovision ==" -ForegroundColor Cyan
Write-Host "Next steps:"
Write-Host "  1. Verify model deployments succeeded (az cognitiveservices account deployment list)."
Write-Host "  2. Verify external DNS records for custom domains (see docs/runbooks/deployment.md)."
Write-Host "  3. Run smoke tests against the gateway and API."
