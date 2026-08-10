<#
.SYNOPSIS
  Tear down named AI4IA resource groups (dry-run by default).

.DESCRIPTION
  Deletes the target resource group(s) and purges soft-deleted Cognitive/Key Vault
  resources so the stack can be rebuilt from IaC. SAFE BY DEFAULT: lists what would
  be deleted unless -Force is supplied. Never touches NetworkWatcherRG,
  Default-ActivityLogAlerts, or DefaultResourceGroup-* (hard-coded protect list).

  -ResourceGroups, -CognitiveAccountNames, and -KeyVaultNames are REQUIRED and
  have no defaults. A
  destructive script must never carry a built-in target: a default resource group
  is wrong the moment the stack moves to another subscription or tenant, and a
  broad purge filter is what turns "clean up my stack" into "purge every
  soft-deleted Cognitive account and Key Vault in this subscription".

  -AcknowledgeDataLoss is REQUIRED alongside -Force. -Force only ever meant "yes,
  really delete the infrastructure", and infrastructure is the part this repo can
  rebuild. The data is not: uploaded documents and generated media have no restore
  path, and the Key Vault holding per-user MCP credentials is purged, not just
  deleted. Separating the two acknowledgements keeps the irreversible one from
  riding along with the routine one. Capture what you need first:

    ./scripts/capture-data-recovery-state.ps1 -Subscription <id> -ResourceGroup <rg>

.EXAMPLE
  ./scripts/teardown.ps1 -Subscription <id> -ResourceGroups rg-ai4ia-<env> -CognitiveAccountNames <foundry-name> -KeyVaultNames <vault-name>
  ./scripts/teardown.ps1 -Subscription <id> -ResourceGroups rg-ai4ia-<env> -CognitiveAccountNames <foundry-name> -KeyVaultNames <vault-name> -Force -AcknowledgeDataLoss
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string] $Subscription,
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string[]] $ResourceGroups,
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
    [switch] $Force,
    [switch] $AcknowledgeDataLoss
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'azure-cli.ps1')
$Protected = @("NetworkWatcherRG", "Default-ActivityLogAlerts", "DefaultResourceGroup-EUS",
               "DefaultResourceGroup-WUS", "DefaultResourceGroup-WUS3", "DefaultResourceGroup-SCUS")

# Checked before anything is enumerated, so the refusal costs nothing and cannot
# be reached halfway through a delete loop.
if ($Force -and -not $AcknowledgeDataLoss) {
    Write-Host ""
    Write-Host "Refusing to run: -Force was supplied without -AcknowledgeDataLoss." -ForegroundColor Red
    Write-Host ""
    Write-Host "This deletes data that no IaC in this repo can rebuild:" -ForegroundColor Yellow
    Write-Host "  - Blob: uploaded documents and generated images/videos. No restore path." -ForegroundColor Yellow
    Write-Host "  - Key Vault: per-user MCP credentials. Purged, not soft-deleted. Unrecoverable." -ForegroundColor Yellow
    Write-Host "  - Cosmos: restorable only if you captured the restorable instance id and a" -ForegroundColor Yellow
    Write-Host "    timestamp BEFORE deletion. Restore targets a new account." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Capture the recovery state first:" -ForegroundColor Cyan
    Write-Host "  ./scripts/capture-data-recovery-state.ps1 -Subscription $Subscription -ResourceGroup $($ResourceGroups[0])" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Then re-run this command with -AcknowledgeDataLoss added." -ForegroundColor Cyan
    exit 2
}

Assert-AzureSubscription -Subscription $Subscription
Write-Host "Subscription: $Subscription" -ForegroundColor Cyan

foreach ($rg in $ResourceGroups) {
    if ($Protected -contains $rg) { Write-Warning "Refusing to delete protected RG: $rg"; continue }
    $exists = Invoke-AzureCli -Arguments @('group', 'exists', '--name', $rg) | ConvertFrom-Json
    if (-not $exists) { Write-Host "  $rg : not found (skip)" -ForegroundColor DarkGray; continue }

    Write-Host "== Resources in $rg ==" -ForegroundColor Cyan
    Invoke-AzureCli -Arguments @(
        'resource', 'list', '--resource-group', $rg,
        '--query', '[].{name:name, type:type}', '--output', 'table'
    )

    if ($Force -or $PSCmdlet.ShouldProcess($rg, "Delete resource group")) {
        Write-Host "  deleting $rg ..." -ForegroundColor Yellow
        Invoke-AzureCli -Arguments @('group', 'delete', '--name', $rg, '--yes') | Out-Null
        Write-Host "  deleted $rg" -ForegroundColor Green
    } else {
        Write-Host "  (dry run) re-run with -Force to delete $rg" -ForegroundColor Yellow
    }
}

if ($Force) {
    Write-Host "== Purging approved soft-deleted Cognitive/Key Vault names ==" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'purge-soft-deleted.ps1') `
        -Subscription $Subscription `
        -CognitiveAccountNames $CognitiveAccountNames `
        -KeyVaultNames $KeyVaultNames `
        -Force
} else {
    Write-Host "Dry run complete. Nothing deleted. Add -Force to execute." -ForegroundColor Yellow
}
