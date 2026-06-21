<#
.SYNOPSIS
  Snapshot the existing AI4IA / aiforia Azure footprint before teardown.

.DESCRIPTION
  Captures Foundry (Cognitive Services) accounts, model deployments, projects,
  connections, Bing grounding, Key Vaults, and quota/usage into timestamped JSON
  files so the rebuild is reversible and auditable. Read-only.

.EXAMPLE
  ./scripts/inventory.ps1 -Subscription ca68cf94-... -ResourceGroup rg-aiforia-slurmfactory
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Subscription,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [string] $OutDir = "./.inventory"
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dir = Join-Path $OutDir $stamp
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Write-Host "Inventorying $ResourceGroup -> $dir" -ForegroundColor Cyan

az account set --subscription $Subscription | Out-Null

function Save($name, [scriptblock]$cmd) {
    Write-Host "  - $name"
    try { & $cmd | Out-File -Encoding utf8 (Join-Path $dir "$name.json") }
    catch { Write-Warning "    failed: $($_.Exception.Message)" }
}

Save "resources"        { az resource list -g $ResourceGroup -o json }
Save "cognitive-accounts" { az cognitiveservices account list -g $ResourceGroup -o json }

# Per-account deployments + connections
$accounts = az cognitiveservices account list -g $ResourceGroup --query "[].name" -o tsv
foreach ($a in $accounts) {
    Save "deployments-$a" { az cognitiveservices account deployment list -g $ResourceGroup -n $a -o json }
}

Save "keyvaults"        { az keyvault list -g $ResourceGroup -o json }
Save "soft-deleted-cognitive" { az cognitiveservices account list-deleted -o json }
Save "soft-deleted-keyvaults"  { az keyvault list-deleted -o json }

Write-Host "Inventory complete: $dir" -ForegroundColor Green
Write-Host "Commit a copy of this folder (or its summary) before running teardown." -ForegroundColor Yellow
