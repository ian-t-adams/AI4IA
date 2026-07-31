<#
.SYNOPSIS
  Tear down named AI4IA resource groups (dry-run by default).

.DESCRIPTION
  Deletes the target resource group(s) and purges soft-deleted Cognitive/Key Vault
  resources so the stack can be rebuilt from IaC. SAFE BY DEFAULT: lists what would
  be deleted unless -Force is supplied. Never touches NetworkWatcherRG,
  Default-ActivityLogAlerts, or DefaultResourceGroup-* (hard-coded protect list).

  -ResourceGroups and -PurgeNameFilter are REQUIRED and have no defaults. A
  destructive script must never carry a built-in target: a default resource group
  is wrong the moment the stack moves to another subscription or tenant, and a
  default purge filter is what turns "clean up my stack" into "purge every
  soft-deleted Cognitive account and Key Vault in this subscription".

.EXAMPLE
  ./scripts/teardown.ps1 -Subscription <id> -ResourceGroups rg-ai4ia-<env> -PurgeNameFilter ai4ia
  ./scripts/teardown.ps1 -Subscription <id> -ResourceGroups rg-ai4ia-<env> -PurgeNameFilter ai4ia -Force
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory)] [string] $Subscription,
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string[]] $ResourceGroups,
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string] $PurgeNameFilter,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
$Protected = @("NetworkWatcherRG", "Default-ActivityLogAlerts", "DefaultResourceGroup-EUS",
               "DefaultResourceGroup-WUS", "DefaultResourceGroup-WUS3", "DefaultResourceGroup-SCUS")

az account set --subscription $Subscription | Out-Null
Write-Host "Subscription: $Subscription" -ForegroundColor Cyan

foreach ($rg in $ResourceGroups) {
    if ($Protected -contains $rg) { Write-Warning "Refusing to delete protected RG: $rg"; continue }
    $exists = az group exists -n $rg | ConvertFrom-Json
    if (-not $exists) { Write-Host "  $rg : not found (skip)" -ForegroundColor DarkGray; continue }

    Write-Host "== Resources in $rg ==" -ForegroundColor Cyan
    az resource list -g $rg --query "[].{name:name, type:type}" -o table

    if ($Force -or $PSCmdlet.ShouldProcess($rg, "Delete resource group")) {
        Write-Host "  deleting $rg ..." -ForegroundColor Yellow
        az group delete -n $rg --yes
        Write-Host "  deleted $rg" -ForegroundColor Green
    } else {
        Write-Host "  (dry run) re-run with -Force to delete $rg" -ForegroundColor Yellow
    }
}

if ($Force) {
    Write-Host "== Purging soft-deleted Cognitive/Key Vault (filter: $PurgeNameFilter) ==" -ForegroundColor Cyan
    & "$PSScriptRoot/purge-soft-deleted.ps1" -Subscription $Subscription -NameFilter $PurgeNameFilter -Force
} else {
    Write-Host "Dry run complete. Nothing deleted. Add -Force to execute." -ForegroundColor Yellow
}
