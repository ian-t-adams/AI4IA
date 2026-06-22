<#
.SYNOPSIS
  Purge soft-deleted Cognitive Services (Foundry) accounts and Key Vaults.

.DESCRIPTION
  `azd down` / `az group delete` leave Cognitive Services accounts and Key Vaults
  in a soft-deleted state, which blocks recreating same-named resources. This purges
  them. Destructive and irreversible - purged data cannot be recovered.

.EXAMPLE
  ./scripts/purge-soft-deleted.ps1 -Subscription ca68cf94-... -NameFilter aiforia -WhatIf
  ./scripts/purge-soft-deleted.ps1 -Subscription ca68cf94-... -NameFilter aiforia -Force
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'NameFilter',
    Justification = 'Consumed inside the Match nested function; the analyzer cannot resolve cross-scope use.')]
param(
    [Parameter(Mandatory)] [string] $Subscription,
    [string] $NameFilter = "",
    [switch] $Force
)

$ErrorActionPreference = "Stop"
az account set --subscription $Subscription | Out-Null

function Match($name) { return [string]::IsNullOrEmpty($NameFilter) -or $name -like "*$NameFilter*" }

Write-Host "== Soft-deleted Cognitive Services accounts ==" -ForegroundColor Cyan
$cog = az cognitiveservices account list-deleted -o json | ConvertFrom-Json
foreach ($c in $cog) {
    if (-not (Match $c.name)) { continue }
    $loc = $c.location
    $rg  = ($c.id -split "/resourceGroups/")[1].Split("/")[0]
    if ($Force -or $PSCmdlet.ShouldProcess("$($c.name) ($loc)", "Purge Cognitive account")) {
        Write-Host "  purging $($c.name) in $loc ..." -ForegroundColor Yellow
        az cognitiveservices account purge --name $c.name --location $loc --resource-group $rg
    }
}

Write-Host "== Soft-deleted Key Vaults ==" -ForegroundColor Cyan
$kv = az keyvault list-deleted -o json | ConvertFrom-Json
foreach ($v in $kv) {
    if (-not (Match $v.name)) { continue }
    if ($Force -or $PSCmdlet.ShouldProcess("$($v.name) ($($v.properties.location))", "Purge Key Vault")) {
        Write-Host "  purging $($v.name) ..." -ForegroundColor Yellow
        az keyvault purge --name $v.name --location $v.properties.location
    }
}

Write-Host "Purge pass complete." -ForegroundColor Green
