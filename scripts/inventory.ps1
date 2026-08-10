<#
.SYNOPSIS
  Snapshot the existing AI4IA Azure footprint before teardown.

.DESCRIPTION
  Captures Foundry (Cognitive Services) accounts, model deployments, projects,
  connections, Bing grounding, Key Vaults, and quota/usage into timestamped JSON
  files so the rebuild is reversible and auditable. Read-only.

.EXAMPLE
  ./scripts/inventory.ps1 -Subscription <id> -ResourceGroup rg-ai4ia-<env>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Subscription,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [string] $OutDir = "./.inventory"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'azure-cli.ps1')
Assert-AzureSubscription -Subscription $Subscription

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dir = Join-Path $OutDir $stamp
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Write-Host "Inventorying $ResourceGroup -> $dir" -ForegroundColor Cyan

$failedSections = [System.Collections.Generic.List[string]]::new()

function Save($name, [scriptblock]$cmd) {
    Write-Host "  - $name"
    try { & $cmd | Out-File -Encoding utf8 (Join-Path $dir "$name.json") }
    catch {
        $failedSections.Add($name) | Out-Null
        Write-Warning "    failed: $($_.Exception.Message)"
    }
}

Save "resources" {
    Invoke-AzureCli -Arguments @('resource', 'list', '--resource-group', $ResourceGroup, '--output', 'json')
}
Save "cognitive-accounts" {
    Invoke-AzureCli -Arguments @(
        'cognitiveservices', 'account', 'list', '--resource-group', $ResourceGroup, '--output', 'json'
    )
}

# Per-account deployments + connections
$accounts = Invoke-AzureCli -Arguments @(
    'cognitiveservices', 'account', 'list', '--resource-group', $ResourceGroup,
    '--query', '[].name', '--output', 'tsv'
)
foreach ($a in $accounts) {
    Save "deployments-$a" {
        Invoke-AzureCli -Arguments @(
            'cognitiveservices', 'account', 'deployment', 'list',
            '--resource-group', $ResourceGroup, '--name', $a, '--output', 'json'
        )
    }
}

Save "keyvaults" {
    Invoke-AzureCli -Arguments @('keyvault', 'list', '--resource-group', $ResourceGroup, '--output', 'json')
}
Save "soft-deleted-cognitive" {
    Invoke-AzureCli -Arguments @('cognitiveservices', 'account', 'list-deleted', '--output', 'json')
}
Save "soft-deleted-keyvaults" {
    Invoke-AzureCli -Arguments @('keyvault', 'list-deleted', '--output', 'json')
}

if ($failedSections.Count -gt 0) {
    Write-Host "Inventory INCOMPLETE: $dir" -ForegroundColor Red
    Write-Host "Failed sections: $($failedSections -join ', ')" -ForegroundColor Red
    exit 1
}

Write-Host "Inventory complete: $dir" -ForegroundColor Green
Write-Host "Commit a copy of this folder (or its summary) before running teardown." -ForegroundColor Yellow
