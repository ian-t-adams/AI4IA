<#
.SYNOPSIS
  Generate the timestamped status/health + resource-inventory data that the self-documenting
  static site (site/status.html and site/services.html) renders.

.DESCRIPTION
  Read-only. Uses Azure Resource Graph as the AUTHORITATIVE source of the deployed
  footprint. This matters: `az resource list -g <rg>` silently OMITS several provider
  types actually present in AI4IA (Microsoft.Search/searchServices,
  Microsoft.ApiCenter/services and the westus/swedencentral Foundry accounts),
  so the generic resources list under-reports
  the environment. Resource Graph's `resources` table returns all of them, and its
  `healthresources` table returns Azure Resource Health availability states in one call.

  The script also probes the public web + proxy ingress URLs for reachability, then
  writes two browser-loadable data files (assigning `window.*` globals, so the static
  site needs no fetch/CORS and works from file:// as well as GitHub Pages):
    site/data/inventory.js  -> window.AI4IA_INVENTORY
    site/data/status.js     -> window.AI4IA_STATUS

  Requires an `az login` with reader access to the target subscription. In CI this is
  the existing federated (OIDC) identity used by deploy.yml.

.PARAMETER Subscription
  Target subscription id. Defaults to the azd environment's AZURE_SUBSCRIPTION_ID,
  then the current `az account show` context. Never a baked-in id -- a hardcoded
  default silently points a new tenant's snapshot at the previous subscription.

.PARAMETER ResourceGroup
  Target resource group. Defaults to the azd environment's AZURE_RESOURCE_GROUP.

.PARAMETER WebUrl / ProxyUrl
  Public ingress URLs probed for reachability. Default to the azd environment's
  AZURE_WEB_URL / AZURE_PROXY_URL outputs (written by `azd provision`). An
  endpoint with no resolvable URL is skipped rather than probed.

.PARAMETER OutDir
  Where the .js data files are written (default: site/data next to this repo).

.EXAMPLE
  ./scripts/status-snapshot.ps1

.EXAMPLE
  ./scripts/status-snapshot.ps1 -Subscription <id> -ResourceGroup rg-ai4ia-<env>
#>
[CmdletBinding()]
param(
    [string] $Subscription  = '',
    [string] $ResourceGroup = '',
    [string] $WebUrl        = '',
    [string] $ProxyUrl      = '',
    [string] $OutDir        = ([System.IO.Path]::Combine(
        (Split-Path -Parent $PSScriptRoot), 'site', 'data'
    ))
)

$ErrorActionPreference = 'Stop'
$nowIso = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

# --- Resolve the target environment ---------------------------------------
# Every value below is discovered, never hardcoded. `azd provision` writes the
# stack's own outputs (AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP /
# AZURE_WEB_URL / AZURE_PROXY_URL) into the selected azd environment, so the
# azd env is the authoritative description of "the deployment this checkout
# points at". That is what makes this script correct in a new tenant or
# subscription with no edits -- and what stops it from quietly snapshotting a
# previous tenant's stack after a move.
$repoRoot = Split-Path -Parent $PSScriptRoot

function Get-AzdEnvValue {
    param([Parameter(Mandatory)][string] $Name)

    if (-not (Get-Command azd -ErrorAction SilentlyContinue)) { return $null }
    try {
        $value = (& azd env get-value $Name --cwd $repoRoot 2>$null | Select-Object -First 1)
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    if ([string]::IsNullOrWhiteSpace($value)) { return $null }
    $value = $value.Trim()
    # azd prints an ERROR line to stdout in some failure modes; treat it as unset.
    if ($value -like 'ERROR:*') { return $null }
    return $value
}

if (-not $Subscription) { $Subscription = Get-AzdEnvValue 'AZURE_SUBSCRIPTION_ID' }
if (-not $Subscription) {
    $Subscription = (az account show --query id -o tsv --only-show-errors 2>$null)
    if ($LASTEXITCODE -ne 0) { $Subscription = $null }
}
if (-not $Subscription) {
    throw 'Could not resolve a subscription. Run `azd env select <env>` (or `az login`), or pass -Subscription <id>.'
}

if (-not $ResourceGroup) { $ResourceGroup = Get-AzdEnvValue 'AZURE_RESOURCE_GROUP' }
if (-not $ResourceGroup) {
    throw 'Could not resolve a resource group. Run `azd env select <env>` after a provision, or pass -ResourceGroup <name>.'
}

if (-not $WebUrl)   { $WebUrl   = Get-AzdEnvValue 'AZURE_WEB_URL' }
if (-not $ProxyUrl) { $ProxyUrl = Get-AzdEnvValue 'AZURE_PROXY_URL' }

Write-Host "Target: subscription $Subscription / resource group $ResourceGroup" -ForegroundColor Cyan

# --- Friendly labels + logical grouping for each Azure resource type. Keeps the
#     status page human-readable and drives the per-group layout on the site. ---
$TypeMap = @{
    'microsoft.apimanagement/service'                       = @{ label = 'API Management';                group = 'Gateway' }
    'microsoft.app/containerapps'                           = @{ label = 'Container App';                 group = 'Compute' }
    'microsoft.app/managedenvironments'                     = @{ label = 'Container Apps Environment';    group = 'Compute' }
    'microsoft.app/managedenvironments/managedcertificates' = @{ label = 'Managed TLS Certificate';       group = 'Compute' }
    'microsoft.appconfiguration/configurationstores'        = @{ label = 'App Configuration';             group = 'Config' }
    'microsoft.cognitiveservices/accounts'                  = @{ label = 'Azure AI Foundry account';      group = 'AI' }
    'microsoft.cognitiveservices/accounts/projects'         = @{ label = 'Foundry project';               group = 'AI' }
    'microsoft.containerregistry/registries'                = @{ label = 'Container Registry';            group = 'Compute' }
    'microsoft.documentdb/databaseaccounts'                 = @{ label = 'Cosmos DB (NoSQL)';             group = 'Data' }
    'microsoft.durabletask/schedulers'                       = @{ label = 'Durable Task Scheduler';        group = 'Compute' }
    'microsoft.search/searchservices'                       = @{ label = 'Azure AI Search';               group = 'Data' }
    'microsoft.eventgrid/systemtopics'                      = @{ label = 'Defender for Storage Event Topic'; group = 'Security' }
    'microsoft.eventhub/namespaces'                         = @{ label = 'Event Hubs (optional telemetry)'; group = 'Messaging' }
    'microsoft.insights/components'                         = @{ label = 'Application Insights';           group = 'Observability' }
    'microsoft.insights/actiongroups'                       = @{ label = 'Monitor Action Group';          group = 'Observability' }
    'microsoft.keyvault/vaults'                             = @{ label = 'Key Vault';                     group = 'Security' }
    'microsoft.managedidentity/userassignedidentities'      = @{ label = 'User-Assigned Managed Identity';group = 'Security' }
    'microsoft.monitor/accounts'                            = @{ label = 'Retained Monitor Workspace (not in IaC)'; group = 'Other' }
    'microsoft.operationalinsights/workspaces'              = @{ label = 'Log Analytics Workspace';       group = 'Observability' }
    'microsoft.storage/storageaccounts'                     = @{ label = 'Storage Account';               group = 'Data' }
    'microsoft.apicenter/services'                          = @{ label = 'API Center';                    group = 'Gateway' }
}

function Resolve-Type([string]$t) {
    if ($TypeMap.ContainsKey($t)) { return $TypeMap[$t] }
    return @{ label = $t; group = 'Other' }
}

Write-Host "Setting subscription $Subscription" -ForegroundColor Cyan
az account set --subscription $Subscription | Out-Null

$graphExt = az extension list --query "[?name=='resource-graph'].name" -o tsv --only-show-errors
if (-not $graphExt) {
    Write-Host 'Installing resource-graph extension...' -ForegroundColor Yellow
    az extension add -n resource-graph -y | Out-Null
}

# --- 1) Authoritative inventory via Resource Graph ---
Write-Host "Querying Resource Graph inventory for $ResourceGroup" -ForegroundColor Cyan
# coalesce provisioningState with `state`: Postgres flexible servers (and a few other
# providers) report readiness under properties.state, not properties.provisioningState.
$invKql = "Resources | where resourceGroup =~ '$ResourceGroup' | project id, name, type, location, prov=tostring(coalesce(properties.provisioningState, properties.state))"
$invOutput = @(az graph query -q $invKql --subscriptions $Subscription --first 500 -o json --only-show-errors 2>&1)
$invExitCode = $LASTEXITCODE
if ($invExitCode -ne 0) {
    throw "Resource Graph inventory query failed (az exit $invExitCode)."
}
try {
    $invRaw = ($invOutput -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw "Resource Graph inventory query returned malformed JSON: $($_.Exception.Message)"
}
$dataProperty = $invRaw.PSObject.Properties['data']
if (-not $dataProperty) {
    throw 'Resource Graph inventory response is missing the required data array.'
}
if ($invRaw.data -isnot [System.Array]) {
    throw 'Resource Graph inventory response data must be an array.'
}
if ($invRaw.data.Count -eq 0) {
    throw "Resource Graph returned no resources for configured resource group '$ResourceGroup'; refusing to publish an empty snapshot."
}
$invalidResources = @($invRaw.data | Where-Object {
    [string]::IsNullOrWhiteSpace($_.id) -or
    [string]::IsNullOrWhiteSpace($_.name) -or
    [string]::IsNullOrWhiteSpace($_.type) -or
    [string]::IsNullOrWhiteSpace($_.location)
})
if ($invalidResources.Count -gt 0) {
    throw "Resource Graph inventory contains $($invalidResources.Count) resource row(s) missing id, name, type, or location."
}

# --- 2) Resource health via Resource Graph healthresources table (one call) ---
Write-Host 'Querying Resource Health availability states' -ForegroundColor Cyan
$health = @{}
$healthSource = [ordered]@{
    status        = 'unavailable'
    provider      = 'Microsoft.ResourceHealth'
    providerState = 'Unknown'
    records       = 0
    note          = 'Azure Resource Health availability was not checked.'
}

$providerOutput = @(
    az provider show --subscription $Subscription --namespace Microsoft.ResourceHealth `
        --query registrationState -o tsv --only-show-errors 2>&1
)
$providerExitCode = $LASTEXITCODE
if ($providerExitCode -ne 0) {
    Write-Warning "Could not verify Microsoft.ResourceHealth registration (az exit $providerExitCode)."
    $healthSource.note = 'Microsoft.ResourceHealth registration could not be verified; availability was not checked.'
} else {
    $providerState = ($providerOutput -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($providerState)) {
        Write-Warning 'Microsoft.ResourceHealth registration query returned no state.'
        $healthSource.note = 'Microsoft.ResourceHealth registration returned no state; availability was not checked.'
    } else {
        $healthSource.providerState = $providerState
        if ($providerState -ine 'Registered') {
            $providerDescription = if ($providerState -ieq 'NotRegistered') {
                'not registered'
            } elseif ($providerState -ieq 'Registering') {
                'still registering'
            } else {
                "in state '$providerState'"
            }
            Write-Warning "Microsoft.ResourceHealth is $providerDescription; availability was not checked."
            $healthSource.note = "Microsoft.ResourceHealth is $providerDescription; availability was not checked."
        } else {
            try {
                # Filter before --first so a busy subscription cannot push this
                # resource group's health rows beyond the 500-row result window.
                $healthResourceGroup = $ResourceGroup.ToLowerInvariant().Replace("'", "''")
                $hKql = "healthresources | where type =~ 'microsoft.resourcehealth/availabilitystatuses' | extend rid=tolower(tostring(properties.targetResourceId)) | where rid contains '/resourcegroups/$healthResourceGroup/' | project rid, state=tostring(properties.availabilityState)"
                $hOutput = @(
                    az graph query -q $hKql --subscriptions $Subscription --first 500 `
                        -o json --only-show-errors 2>&1
                )
                $hExitCode = $LASTEXITCODE
                if ($hExitCode -ne 0) { throw "az exited $hExitCode" }
                $hRaw = ($hOutput -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
                if ($hRaw.data -isnot [System.Array]) { throw 'response data is not an array' }
                foreach ($h in $hRaw.data) {
                    if ($h.rid) { $health[$h.rid] = $h.state }
                }
                $healthSource.status = 'available'
                $healthSource.records = $health.Count
                $healthSource.note = if ($health.Count -eq 0) {
                    'Resource Health query succeeded; no resource published an availability state.'
                } else {
                    "Resource Health query returned $($health.Count) availability state(s)."
                }
            } catch {
                Write-Warning "Resource Health query failed (continuing without it): $($_.Exception.Message)"
                $healthSource.note = 'Azure Resource Health could not be queried; availability was not checked.'
            }
        }
    }
}

# --- 3) Shape the resource list ---
# state classification (drives the status colours on the site):
#   unavailable -> Resource Health reports Unavailable
#   degraded    -> Resource Health reports Degraded, or provisioning Failed/Canceled
#   healthy     -> Resource Health positively reports Available
#   provisioned -> present in Resource Graph and not failed/unavailable, but
#                  Resource Health has no opinion (most resource types never
#                  publish an availability state at all)
#
# 'provisioned' is deliberately distinct from 'healthy'. This previously
# collapsed both into 'healthy', so the portal could report "33 Healthy" while
# every single row displayed "Availability: Unknown" -- existence was being
# presented as health. Absence of a signal is not a positive signal.
$resources = @(
    foreach ($r in ($invRaw.data | Sort-Object type, name)) {
        $meta  = Resolve-Type $r.type
        $rid   = ($r.id).ToLower()
        $healthReported = $health.ContainsKey($rid)
        $avail = if ($healthReported) { $health[$rid] } else { 'Unknown' }
        $prov  = if ([string]::IsNullOrWhiteSpace($r.prov)) { 'n/a' } else { $r.prov }
        $state = if ($avail -eq 'Available') { 'healthy' } else { 'provisioned' }
        if ($avail -eq 'Unavailable') { $state = 'unavailable' }
        elseif ($avail -eq 'Degraded') { $state = 'degraded' }
        elseif ($prov -in 'Failed','Canceled') { $state = 'degraded' }
        [pscustomobject]@{
            name              = $r.name
            type              = $r.type
            label             = $meta.label
            group             = $meta.group
            location          = $r.location
            provisioningState = $prov
            availability      = $avail
            healthReported    = $healthReported
            state             = $state
        }
    }
)

# --- 4) Probe public endpoints for reachability ---
function Test-Endpoint([string]$name, [string]$url) {
    $obj = [ordered]@{ name = $name; url = $url; httpStatus = 0; ok = $false; state = 'down'; note = '' }
    try {
        $resp = Invoke-WebRequest -Uri $url -Method Get -TimeoutSec 20 -MaximumRedirection 5 -SkipHttpErrorCheck -UseBasicParsing
        $obj.httpStatus = [int]$resp.StatusCode
        # 2xx/3xx, or a 401/403 (auth challenge) all prove the ingress is up and serving.
        $obj.ok = ($obj.httpStatus -ge 200 -and $obj.httpStatus -lt 400) -or ($obj.httpStatus -in 401,403)
        if ($obj.ok) { $obj.state = 'up' } else { $obj.state = 'down' }
        if ($obj.httpStatus -in 401,403) { $obj.note = 'reachable (auth required)' }
    } catch {
        # No HTTP response at all (timeout / connection reset). For a scale-to-zero
        # ingress a cold-start timeout is inconclusive, not a confirmed outage.
        $obj.state = 'unknown'
        $obj.note  = 'no response (timeout or cold start)'
    }
    [pscustomobject]$obj
}
Write-Host 'Probing public endpoints' -ForegroundColor Cyan
# An endpoint whose URL could not be resolved is reported as 'unknown' rather than
# probed: a missing azd output is an unknown, not a confirmed outage.
function Get-UnresolvedEndpoint([string]$name) {
    [pscustomobject][ordered]@{
        name = $name; url = ''; httpStatus = 0; ok = $false; state = 'unknown'
        note = 'no URL resolved (set the azd env output or pass the parameter)'
    }
}
$endpoints = @(
    $(if ($WebUrl)   { Test-Endpoint 'Web app'     $WebUrl }   else { Get-UnresolvedEndpoint 'Web app' }),
    $(if ($ProxyUrl) { Test-Endpoint 'Model proxy' $ProxyUrl } else { Get-UnresolvedEndpoint 'Model proxy' })
)

# --- 5) Summaries ---
$summary = [ordered]@{
    total        = $resources.Count
    healthy      = ($resources | Where-Object { $_.state -eq 'healthy' }).Count
    # Provisioned-but-unreported. Counted separately so the portal can never
    # again present "no availability signal" as "healthy".
    provisioned  = ($resources | Where-Object { $_.state -eq 'provisioned' }).Count
    degraded     = ($resources | Where-Object { $_.state -eq 'degraded' }).Count
    unavailable  = ($resources | Where-Object { $_.state -eq 'unavailable' }).Count
    endpointsUp  = ($endpoints | Where-Object { $_.ok }).Count
    endpointsTot = $endpoints.Count
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$inventory = [ordered]@{
    generatedAt   = $nowIso
    subscription  = $Subscription
    resourceGroup = $ResourceGroup
    resources     = $resources
}
$status = [ordered]@{
    generatedAt   = $nowIso
    subscription  = $Subscription
    resourceGroup = $ResourceGroup
    healthSource  = $healthSource
    summary       = $summary
    endpoints     = $endpoints
}

$header = "// AUTO-GENERATED by scripts/status-snapshot.ps1 at $nowIso. Do not edit by hand."
$invJson = ($inventory | ConvertTo-Json -Depth 8)
$stsJson = ($status    | ConvertTo-Json -Depth 8)

Set-Content -Path (Join-Path $OutDir 'inventory.js') -Encoding utf8 -Value "$header`nwindow.AI4IA_INVENTORY = $invJson;"
Set-Content -Path (Join-Path $OutDir 'status.js')    -Encoding utf8 -Value "$header`nwindow.AI4IA_STATUS = $stsJson;"

Write-Host "Wrote inventory.js + status.js to $OutDir" -ForegroundColor Green
Write-Host ("Resources: {0}  Healthy: {1}  Resource Health: {2}  Endpoints up: {3}/{4}" -f `
    $summary.total, $summary.healthy, $healthSource.status, `
    $summary.endpointsUp, $summary.endpointsTot) -ForegroundColor Green
