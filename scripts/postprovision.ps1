<#
.SYNOPSIS
  azd postprovision hook (placeholder for Phase 0/1).

.DESCRIPTION
  Runs after `azd provision`. Currently prints follow-up steps. Later phases wire:
   - DNS CNAME/TXT validation for ai4ia.nomad-analytics.com / genaiproxy.nomad-analytics.com
   - model-catalog seeding / verification
   - smoke checks against the deployed gateway + api
  Non-fatal by design (azure.yaml sets continueOnError: true).
#>
$ErrorActionPreference = "Continue"
Write-Host "== AI4IA postprovision ==" -ForegroundColor Cyan
Write-Host "Next steps:"
Write-Host "  1. Verify model deployments succeeded (az cognitiveservices account deployment list)."
Write-Host "  2. Configure DNS records for custom domains (see docs/runbooks/dns.md)."
Write-Host "  3. Run smoke tests against the gateway and API."
