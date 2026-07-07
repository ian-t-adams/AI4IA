<#
.SYNOPSIS
  Generate the live status/health + resource-inventory data that the self-documenting
  static site (site/status.html and site/services.html) renders.

.DESCRIPTION
  Read-only. Uses Azure Resource Graph as the AUTHORITATIVE source of the deployed
  footprint. This matters: `az resource list -g <rg>` silently OMITS several provider
  types actually present in AI4IA (Microsoft.Search/searchServices,
  Microsoft.DBforPostgreSQL/flexibleServers, Microsoft.ApiCenter/services and the
  westus/swedencentral Foundry accounts), so the generic resources list under-reports
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
  Target subscription id (default: the AI4IA live subscription).

.PARAMETER ResourceGroup
  Target resource group (default: rg-ai4ia-slurmfactory).

.PARAMETER WebUrl / ProxyUrl
  Public ingress URLs probed for reachability.

.PARAMETER OutDir
  Where the .js data files are written (default: site/data next to this repo).

.EXAMPLE
  ./scripts/status-snapshot.ps1
#>
[CmdletBinding()]
param(
    [string] $Subscription  = 'ca68cf94-f445-43f1-8379-3d0100e293a2',
    [string] $ResourceGroup = 'rg-ai4ia-slurmfactory',
    [string] $WebUrl        = 'https://ai4ia.nomad-analytics.com',
    [string] $ProxyUrl      = 'https://genaiproxy.nomad-analytics.com',
    [string] $OutDir        = (Join-Path $PSScriptRoot '..\site\data')
)

$ErrorActionPreference = 'Stop'
$nowIso = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

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
    'microsoft.dbforpostgresql/flexibleservers'             = @{ label = 'Postgres Flexible Server';      group = 'Data' }
    'microsoft.search/searchservices'                       = @{ label = 'Azure AI Search';               group = 'Data' }
    'microsoft.eventgrid/systemtopics'                      = @{ label = 'Event Grid System Topic';       group = 'Messaging' }
    'microsoft.eventhub/namespaces'                         = @{ label = 'Event Hubs Namespace';          group = 'Messaging' }
    'microsoft.insights/components'                         = @{ label = 'Application Insights';           group = 'Observability' }
    'microsoft.insights/actiongroups'                       = @{ label = 'Monitor Action Group';          group = 'Observability' }
    'microsoft.keyvault/vaults'                             = @{ label = 'Key Vault';                     group = 'Security' }
    'microsoft.managedidentity/userassignedidentities'      = @{ label = 'User-Assigned Managed Identity';group = 'Security' }
    'microsoft.monitor/accounts'                            = @{ label = 'Azure Monitor Workspace';       group = 'Observability' }
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
$invRaw = az graph query -q $invKql --first 500 -o json | ConvertFrom-Json

# --- 2) Resource health via Resource Graph healthresources table (one call) ---
Write-Host 'Querying Resource Health availability states' -ForegroundColor Cyan
$health = @{}
try {
    $hKql = "healthresources | where type =~ 'microsoft.resourcehealth/availabilitystatuses' | project rid=tolower(tostring(properties.targetResourceId)), state=tostring(properties.availabilityState)"
    $hRaw = az graph query -q $hKql --first 500 -o json --only-show-errors | ConvertFrom-Json
    foreach ($h in $hRaw.data) { if ($h.rid) { $health[$h.rid] = $h.state } }
} catch {
    Write-Warning "Resource Health query failed (continuing without it): $($_.Exception.Message)"
}

# --- 3) Shape the resource list ---
# state classification (drives the status colours on the site):
#   unavailable -> Resource Health reports Unavailable
#   degraded    -> provisioning Failed/Canceled
#   healthy     -> present in Resource Graph and not failed/unavailable (existence == provisioned)
$resources = foreach ($r in ($invRaw.data | Sort-Object type, name)) {
    $meta  = Resolve-Type $r.type
    $rid   = ($r.id).ToLower()
    $avail = if ($health.ContainsKey($rid)) { $health[$rid] } else { 'Unknown' }
    $prov  = if ([string]::IsNullOrWhiteSpace($r.prov)) { 'n/a' } else { $r.prov }
    $state = 'healthy'
    if ($avail -eq 'Unavailable') { $state = 'unavailable' }
    elseif ($prov -in 'Failed','Canceled') { $state = 'degraded' }
    [pscustomobject]@{
        name              = $r.name
        type              = $r.type
        label             = $meta.label
        group             = $meta.group
        location          = $r.location
        provisioningState = $prov
        availability      = $avail
        state             = $state
    }
}

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
$endpoints = @(
    (Test-Endpoint 'Web app'      $WebUrl),
    (Test-Endpoint 'Model proxy'  $ProxyUrl)
)

# --- 5) Summaries ---
$summary = [ordered]@{
    total        = $resources.Count
    healthy      = ($resources | Where-Object { $_.state -eq 'healthy' }).Count
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
    summary       = $summary
    endpoints     = $endpoints
    resources     = $resources
}

$header = "// AUTO-GENERATED by scripts/status-snapshot.ps1 at $nowIso. Do not edit by hand."
$invJson = ($inventory | ConvertTo-Json -Depth 8)
$stsJson = ($status    | ConvertTo-Json -Depth 8)

Set-Content -Path (Join-Path $OutDir 'inventory.js') -Encoding utf8 -Value "$header`nwindow.AI4IA_INVENTORY = $invJson;"
Set-Content -Path (Join-Path $OutDir 'status.js')    -Encoding utf8 -Value "$header`nwindow.AI4IA_STATUS = $stsJson;"

Write-Host "Wrote inventory.js + status.js to $OutDir" -ForegroundColor Green
Write-Host ("Resources: {0}  Healthy: {1}  Endpoints up: {2}/{3}" -f `
    $summary.total, $summary.healthy, $summary.endpointsUp, $summary.endpointsTot) -ForegroundColor Green
