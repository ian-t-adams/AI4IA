<#
.SYNOPSIS
  Refresh the per-region model availability snapshots used to curate infra/models.json.

.DESCRIPTION
  Calls `az cognitiveservices model list` for each candidate region and writes the raw
  JSON to ./.modelcache. Use these snapshots to re-curate infra/models.json when Azure
  publishes new models or SKUs. Read-only against Azure.

.EXAMPLE
  ./scripts/seed-models.ps1 -Subscription <id>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Subscription,
    [string[]] $Regions = @("eastus", "eastus2", "westus", "westus3", "swedencentral"),
    [string] $OutDir = "./.modelcache"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'azure-cli.ps1')
Assert-AzureSubscription -Subscription $Subscription
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

foreach ($r in $Regions) {
    Write-Host "Fetching deployable models in $r ..." -ForegroundColor Cyan
    $path = Join-Path $OutDir "models-$r.json"
    Invoke-AzureCli -Arguments @(
        'cognitiveservices', 'model', 'list', '--location', $r, '--output', 'json'
    ) | Out-File -Encoding utf8 $path
    $count = (Get-Content $path -Raw | ConvertFrom-Json).Count
    Write-Host "  $r -> $count entries ($path)"
}

Write-Host "Done. Re-curate infra/models.json from these snapshots as needed." -ForegroundColor Green
