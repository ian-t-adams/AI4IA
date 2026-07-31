<#
.SYNOPSIS
  Create (or verify) the two Microsoft Entra app registrations AI4IA's production
  Entra auth needs, and print the repository-variable values that wire them.

.DESCRIPTION
  AI4IA's `AI4IA_AUTH_PROVIDER=entra` mode validates user bearer tokens (see
  app/api/src/ai4ia_api/auth/entra.py) and signs users in with MSAL in the browser.
  That needs two tenant app registrations the Bicep does NOT create:

    1. an API app that exposes a delegated `access_as_user` scope (v2 tokens), and
    2. a web single-page-application (SPA) client with the site's redirect URI, granted
       that scope.

  The `AI4IA_ENTRA_*` repository variables (see docs/runbooks/deployment.md 2.7) point at
  these two apps. This script makes a fresh new-tenant stand-up turnkey instead of manual.

  READ-ONLY BY DEFAULT. It prints the plan and, if the apps already exist, their current
  values. Pass -Apply to create/patch. It is idempotent: existing apps (matched by display
  name) are reused, and the `access_as_user` scope / redirect URIs / delegated grant are
  added only if missing.

  Requires an `az login` in the target tenant with permission to create app registrations
  and grant admin consent (e.g. Application Administrator / Cloud Application Administrator).

.PARAMETER TenantId
  Target Entra tenant id. Defaults to the signed-in `az` tenant.

.PARAMETER ApiDisplayName
  Display name for the API app registration. Default: "AI4IA API".

.PARAMETER WebDisplayName
  Display name for the web SPA app registration. Default: "AI4IA Web".

.PARAMETER WebRedirectUri
  SPA redirect URIs for the browser client. Pass the vanity host (e.g.
  https://ai4ia.example.com) plus any dev origin. Default: http://localhost:3000.

.PARAMETER AdminUpn
  Optional user principal name; its object id is emitted as AI4IA_ADMIN_SUBJECTS.

.PARAMETER Apply
  Actually create/patch the registrations. Omit for a dry run.

.EXAMPLE
  ./scripts/provision-entra-apps.ps1 -WebRedirectUri https://ai4ia.example.com,http://localhost:3000

.EXAMPLE
  ./scripts/provision-entra-apps.ps1 -WebRedirectUri https://ai4ia.example.com -AdminUpn me@example.com -Apply
#>
[CmdletBinding()]
param(
    [string]$TenantId,
    [string]$ApiDisplayName = 'AI4IA API',
    [string]$WebDisplayName = 'AI4IA Web',
    [string[]]$WebRedirectUri = @('http://localhost:3000'),
    [string]$AdminUpn,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$graph = 'https://graph.microsoft.com/v1.0'

function Invoke-Graph {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [object]$Body
    )
    $azArgs = @('rest', '--method', $Method, '--url', $Url, '--headers', 'Content-Type=application/json')
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = New-TemporaryFile
        ($Body | ConvertTo-Json -Depth 12) | Set-Content -Path $tmp -Encoding utf8
        $azArgs += @('--body', "@$tmp")
    }
    try {
        $out = az @azArgs 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Graph $Method $Url failed (az exit $LASTEXITCODE)." }
        if ([string]::IsNullOrWhiteSpace($out)) { return $null }
        return ($out | ConvertFrom-Json)
    }
    finally {
        if ($tmp) { Remove-Item -Path $tmp -ErrorAction SilentlyContinue }
    }
}

function Get-AppByName {
    param([Parameter(Mandatory)][string]$DisplayName)
    $found = az ad app list --display-name $DisplayName --query '[0]' 2>$null | ConvertFrom-Json
    return $found
}

function Write-Section { param([string]$Text) Write-Host "`n=== $Text ===" -ForegroundColor Cyan }

# --- Preamble -------------------------------------------------------------------------
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) { throw "Not logged in. Run 'az login' in the target tenant first." }
if (-not $TenantId) { $TenantId = $account.tenantId }
$mode = if ($Apply) { 'APPLY' } else { 'DRY RUN (read-only; pass -Apply to make changes)' }
Write-Host "AI4IA Entra app registrations -- $mode" -ForegroundColor Yellow
Write-Host "Tenant: $TenantId"
Write-Host "Signed in as: $($account.user.name)"

# --- API app --------------------------------------------------------------------------
Write-Section 'API app (audience + access_as_user scope)'
$api = Get-AppByName -DisplayName $ApiDisplayName
if (-not $api) {
    Write-Host "API app '$ApiDisplayName' does not exist yet."
    if ($Apply) {
        $api = az ad app create --display-name $ApiDisplayName --sign-in-audience AzureADMyOrg | ConvertFrom-Json
        if (-not $api.appId) { throw "Failed to create API app '$ApiDisplayName'." }
        Write-Host "Created API app $($api.appId)."
    }
}
else {
    Write-Host "Reusing existing API app $($api.appId)."
}

$apiAppId = if ($api) { $api.appId } else { '<created-on-apply>' }
$apiObjId = if ($api) { $api.id } else { $null }
$existingScope = $null
if ($api -and $api.api -and $api.api.oauth2PermissionScopes) {
    $existingScope = $api.api.oauth2PermissionScopes | Where-Object { $_.value -eq 'access_as_user' } | Select-Object -First 1
}
$scopeId = if ($existingScope) { $existingScope.id } else { [guid]::NewGuid().Guid }

if ($existingScope) {
    Write-Host "access_as_user scope already exposed (id $scopeId)."
}
elseif ($Apply -and $apiObjId) {
    $patch = @{
        identifierUris = @("api://$apiAppId")
        api            = @{
            requestedAccessTokenVersion = 2
            oauth2PermissionScopes      = @(@{
                    id                      = $scopeId
                    value                   = 'access_as_user'
                    type                    = 'User'
                    isEnabled               = $true
                    adminConsentDisplayName = 'Access AI4IA as the signed-in user'
                    adminConsentDescription = 'Allow the client to call the AI4IA API on behalf of the signed-in user.'
                    userConsentDisplayName  = 'Access AI4IA on your behalf'
                    userConsentDescription  = 'Allow the client to call the AI4IA API on your behalf.'
                })
        }
    }
    Invoke-Graph -Method PATCH -Url "$graph/applications/$apiObjId" -Body $patch | Out-Null
    az ad sp create --id $apiAppId 2>$null | Out-Null
    Write-Host "Exposed api://$apiAppId/access_as_user (v2 tokens)."
}
else {
    Write-Host "Would set identifierUri api://$apiAppId and expose access_as_user (scope id $scopeId)."
}

# --- Web SPA app ----------------------------------------------------------------------
Write-Section 'Web SPA app (redirect URIs + delegated permission)'
$web = Get-AppByName -DisplayName $WebDisplayName
if (-not $web) {
    Write-Host "Web SPA app '$WebDisplayName' does not exist yet."
    if ($Apply) {
        # `az ad app create` has no SPA flag at all -- spa.redirectUris is Graph-only. Creating
        # the app through Graph sets the URIs in the same call, so a failure cannot leave a
        # redirect-less app behind that would then be "reused" on the next run.
        $web = Invoke-Graph -Method POST -Url "$graph/applications" -Body @{
            displayName    = $WebDisplayName
            signInAudience = 'AzureADMyOrg'
            spa            = @{ redirectUris = @($WebRedirectUri) }
        }
        if (-not $web.appId) { throw "Failed to create web SPA app '$WebDisplayName'." }
        Write-Host "Created web SPA app $($web.appId) with redirect URIs: $($WebRedirectUri -join ', ')."
    }
}
else {
    Write-Host "Reusing existing web SPA app $($web.appId)."
    $haveUris = @($web.spa.redirectUris)
    $missingUris = @($WebRedirectUri | Where-Object { $_ -notin $haveUris })
    if ($missingUris) {
        if ($Apply) {
            Invoke-Graph -Method PATCH -Url "$graph/applications/$($web.id)" `
                -Body @{ spa = @{ redirectUris = @($haveUris + $missingUris) } } | Out-Null
            Write-Host "Added redirect URI(s): $($missingUris -join ', ')."
        }
        else {
            Write-Host "Would add redirect URI(s): $($missingUris -join ', ')."
        }
    }
}
$webAppId = if ($web) { $web.appId } else { '<created-on-apply>' }
$webObjId = if ($web) { $web.id } else { $null }

if ($Apply -and $webObjId) {
    $rra = @{
        requiredResourceAccess = @(@{
                resourceAppId  = $apiAppId
                resourceAccess = @(@{ id = $scopeId; type = 'Scope' })
            })
    }
    Invoke-Graph -Method PATCH -Url "$graph/applications/$webObjId" -Body $rra | Out-Null
    az ad sp create --id $webAppId 2>$null | Out-Null
    az ad app permission admin-consent --id $webAppId 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Granted the web SPA delegated access_as_user with admin consent."
    }
    else {
        Write-Warning ("Delegated access_as_user was recorded on the app, but ADMIN CONSENT FAILED " +
            "(requires Privileged Role Administrator, Cloud Application Administrator, or Global " +
            "Administrator -- subscription Owner is not enough). Sign-in will prompt each user, or " +
            "fail if user consent is disabled. Grant it at: Entra -> App registrations -> " +
            "$WebDisplayName -> API permissions -> Grant admin consent.")
    }
}
elseif (-not $Apply) {
    Write-Host "Would grant the web SPA delegated access_as_user on the API app and admin-consent it."
}

# --- Admin subject --------------------------------------------------------------------
$adminOid = ''
if ($AdminUpn) {
    Write-Section 'Admin subject'
    $adminOid = az ad user show --id $AdminUpn --query id -o tsv 2>$null
    if ($adminOid) { Write-Host "$AdminUpn -> $adminOid" } else { Write-Host "Could not resolve $AdminUpn (set AI4IA_ADMIN_SUBJECTS manually)." }
}

# --- Repository variable values -------------------------------------------------------
Write-Section 'Repository variables (Settings -> Secrets and variables -> Actions -> Variables)'
Write-Host "AI4IA_ENTRA_TENANT_ID    = $TenantId"
Write-Host "AI4IA_ENTRA_AUDIENCE     = $apiAppId"
Write-Host "AI4IA_ENTRA_API_SCOPE    = api://$apiAppId/access_as_user"
Write-Host "AI4IA_ENTRA_WEB_CLIENT_ID= $webAppId"
if ($adminOid) { Write-Host "AI4IA_ADMIN_SUBJECTS     = $adminOid" }
Write-Host ''
if (-not $Apply) {
    Write-Host 'Dry run only -- re-run with -Apply to create/patch the registrations.' -ForegroundColor Yellow
}
else {
    Write-Host 'Done. Verify both apps in the Entra portal, then set the variables above and deploy.' -ForegroundColor Green
}
