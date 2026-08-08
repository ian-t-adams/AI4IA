<#
.SYNOPSIS
  Remove resources intentionally retained by the Lean Azure IaC migration.

.DESCRIPTION
  ARM incremental deployments do not delete resources removed from Bicep or
  resources hidden behind a newly disabled condition. This one-time operator
  script removes only three explicitly supplied, fully qualified resource IDs:
  the retired Event Hubs namespace, the retired Monitor workspace, and the
  portal-created API Center swagger-petstore sample.

  The script is dry-run-only unless both -Execute and
  -AcknowledgeRetainedResourceDeletion are supplied. It first verifies all
  targets, then removes direct role assignments at the exact Event Hubs
  namespace scope before deleting the resources. It is deliberately not wired
  to azd or deployment hooks; changing a feature flag never deletes live Azure.

.EXAMPLE
  ./scripts/cleanup-lean-azure-retained.ps1 `
    -EventHubsNamespaceResourceId '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.EventHub/namespaces/<namespace>' `
    -MonitorWorkspaceResourceId '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Monitor/accounts/<workspace>' `
    -ApiCenterSampleApiResourceId '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiCenter/services/<service>/workspaces/default/apis/swagger-petstore'

.EXAMPLE
  ./scripts/cleanup-lean-azure-retained.ps1 `
    -EventHubsNamespaceResourceId '<exact-id>' `
    -MonitorWorkspaceResourceId '<exact-id>' `
    -ApiCenterSampleApiResourceId '<exact-id>' `
    -Execute -AcknowledgeRetainedResourceDeletion
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft\.EventHub/namespaces/[^/]+$')]
    [string] $EventHubsNamespaceResourceId,

    [Parameter(Mandatory)]
    [ValidatePattern('(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft\.Monitor/accounts/[^/]+$')]
    [string] $MonitorWorkspaceResourceId,

    [Parameter(Mandatory)]
    [ValidatePattern('(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft\.ApiCenter/services/[^/]+/workspaces/default/apis/swagger-petstore$')]
    [string] $ApiCenterSampleApiResourceId,

    [switch] $Execute,
    [switch] $AcknowledgeRetainedResourceDeletion
)

$ErrorActionPreference = 'Stop'

function Get-DeploymentScope {
    param([Parameter(Mandatory)] [string] $ResourceId)
    $match = [regex]::Match(
        $ResourceId,
        '(?i)^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/'
    )
    if (-not $match.Success) {
        throw "Resource ID is not scoped to a subscription resource group: $ResourceId"
    }
    return "$($match.Groups[1].Value.ToLowerInvariant())/$($match.Groups[2].Value.ToLowerInvariant())"
}

function Invoke-AzureCli {
    param([Parameter(Mandatory)] [string[]] $Arguments)
    $output = & az @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed: az $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return $output
}

$targets = @(
    $EventHubsNamespaceResourceId,
    $MonitorWorkspaceResourceId,
    $ApiCenterSampleApiResourceId
)
$scopes = @($targets | ForEach-Object { Get-DeploymentScope -ResourceId $_ } | Select-Object -Unique)
if ($scopes.Count -ne 1) {
    throw 'All cleanup targets must belong to the same subscription and resource group.'
}

Write-Host 'Lean Azure retained-resource cleanup plan:' -ForegroundColor Cyan
$targets | ForEach-Object { Write-Host "  $_" }

if (-not $Execute) {
    Write-Host 'Dry run complete. Nothing deleted.' -ForegroundColor Yellow
    Write-Host 'Re-run with -Execute -AcknowledgeRetainedResourceDeletion after verifying every ID.' -ForegroundColor Yellow
    return
}

if (-not $AcknowledgeRetainedResourceDeletion) {
    throw 'Refusing deletion: -Execute requires -AcknowledgeRetainedResourceDeletion.'
}

# Verify every exact target before the first destructive operation so a typo
# cannot leave this one-time migration half-complete.
foreach ($target in $targets) {
    Invoke-AzureCli -Arguments @('resource', 'show', '--ids', $target, '--only-show-errors', '--output', 'none') | Out-Null
}

$roleOutput = Invoke-AzureCli -Arguments @(
    'role', 'assignment', 'list',
    '--scope', $EventHubsNamespaceResourceId,
    '--include-inherited', 'false',
    '--query', '[].id',
    '--output', 'tsv',
    '--only-show-errors'
)
$roleAssignmentIds = @($roleOutput | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
foreach ($roleAssignmentId in $roleAssignmentIds) {
    Invoke-AzureCli -Arguments @('role', 'assignment', 'delete', '--ids', $roleAssignmentId, '--only-show-errors') | Out-Null
}

foreach ($target in @($ApiCenterSampleApiResourceId, $EventHubsNamespaceResourceId, $MonitorWorkspaceResourceId)) {
    Invoke-AzureCli -Arguments @('resource', 'delete', '--ids', $target, '--only-show-errors') | Out-Null
}

Write-Host "Removed $($roleAssignmentIds.Count) Event Hubs role assignment(s) and all three retained resources." -ForegroundColor Green
