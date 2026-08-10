<#
.SYNOPSIS
  Purge soft-deleted Cognitive Services (Foundry) accounts and Key Vaults.

.DESCRIPTION
  `azd down` / `az group delete` leave Cognitive Services accounts and Key Vaults
  in a soft-deleted state, which blocks recreating same-named resources. This purges
  them. Destructive and irreversible - purged data cannot be recovered.

  -CognitiveAccountNames and -KeyVaultNames are REQUIRED and accept only exact
  resource names. Keeping approvals typed prevents a same-named resource of the
  other kind from being purged. The soft-delete lists it reads
  (`az cognitiveservices account list-deleted` / `az keyvault list-deleted`) are
  SUBSCRIPTION-wide, not scoped to a resource group. Wildcards are rejected so
  this script cannot accidentally approve resources owned by other stacks.

.EXAMPLE
  ./scripts/purge-soft-deleted.ps1 -Subscription <id> -CognitiveAccountNames <foundry-name> -KeyVaultNames <vault-name> -WhatIf
  ./scripts/purge-soft-deleted.ps1 -Subscription <id> -CognitiveAccountNames <foundry-name> -KeyVaultNames <vault-name> -Force
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string] $Subscription,
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()]
    [ValidateScript({
        if ([string]::IsNullOrWhiteSpace($_) -or $_.IndexOfAny([char[]] '*?') -ge 0) {
            throw 'Purge names must be exact non-wildcard resource names.'
        }
        return $true
    })]
    [string[]] $CognitiveAccountNames,
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()]
    [ValidateScript({
        if ([string]::IsNullOrWhiteSpace($_) -or $_.IndexOfAny([char[]] '*?') -ge 0) {
            throw 'Purge names must be exact non-wildcard resource names.'
        }
        return $true
    })]
    [string[]] $KeyVaultNames,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'azure-cli.ps1')
Assert-AzureSubscription -Subscription $Subscription

Write-Host "== Soft-deleted Cognitive Services accounts ==" -ForegroundColor Cyan
$cog = Invoke-AzureCli -Arguments @(
    'cognitiveservices', 'account', 'list-deleted', '--output', 'json'
) | ConvertFrom-Json
foreach ($c in $cog) {
    if ($CognitiveAccountNames -notcontains $c.name) { continue }
    $loc = $c.location
    $rg  = ($c.id -split "/resourceGroups/")[1].Split("/")[0]
    if ($Force -or $PSCmdlet.ShouldProcess("$($c.name) ($loc)", "Purge Cognitive account")) {
        Write-Host "  purging $($c.name) in $loc ..." -ForegroundColor Yellow
        Invoke-AzureCli -Arguments @(
            'cognitiveservices', 'account', 'purge',
            '--name', $c.name, '--location', $loc, '--resource-group', $rg
        ) | Out-Null
    }
}

Write-Host "== Soft-deleted Key Vaults ==" -ForegroundColor Cyan
$kv = Invoke-AzureCli -Arguments @('keyvault', 'list-deleted', '--output', 'json') | ConvertFrom-Json
foreach ($v in $kv) {
    if ($KeyVaultNames -notcontains $v.name) { continue }
    if ($Force -or $PSCmdlet.ShouldProcess("$($v.name) ($($v.properties.location))", "Purge Key Vault")) {
        Write-Host "  purging $($v.name) ..." -ForegroundColor Yellow
        Invoke-AzureCli -Arguments @(
            'keyvault', 'purge', '--name', $v.name, '--location', $v.properties.location
        ) | Out-Null
    }
}

Write-Host "Purge pass complete." -ForegroundColor Green
