#requires -Version 5.1
<#
.SYNOPSIS
  Post-provision smoke tests for AI4IA. Fails LOUDLY (non-zero exit) when a
  resource that SHOULD exist after `azd provision` is missing or unhealthy, so a
  broken deploy can no longer look successful.

.DESCRIPTION
  Runs in the azd `postprovision` hook (see azure.yaml). azd injects every infra
  output into this process's environment (AZURE_API_URL, AZURE_RESOURCE_GROUP,
  AZURE_SUBSCRIPTION_ID, AZURE_FOUNDRY_ENDPOINTS, ...). Values are read from the
  environment first and fall back to `azd env get-values`.

  Checks (each conditional on the relevant resource/var actually existing -
  fail-closed only when the resource SHOULD exist):

    1. Model deployments (HARD GATE). The Foundry/OpenAI accounts and their model
       deployments are created by `azd provision` itself, so immediately after
       provision they MUST exist and report provisioningState == 'Succeeded'.
       Queried via the ARM REST API with a token from `azd auth token` (falls back
       to `az account get-access-token`). If no token can be obtained the check is
       a loud WARN ("cannot evaluate" != broken), never a silent pass.

    2. API health (BEST-EFFORT by default). GET {AZURE_API_URL}/health/live and
       /health/ready. NOTE: postprovision runs BEFORE `azd deploy`, so on a
       greenfield (or provision-only) run the api container may still be the azd
       placeholder image (mcr.microsoft.com/k8se/quickstart) which has no /health
       route. A failure is therefore a WARN by default to avoid false negatives.
       Set AI4IA_SMOKE_REQUIRE_API_HEALTH=true (e.g. when invoked as a post-deploy
       smoke) to promote it to a hard gate.

    3. Custom-domain DNS (BEST-EFFORT). When AI4IA_WEB_CUSTOM_DOMAIN /
       AI4IA_PROXY_CUSTOM_DOMAIN are set, confirm the hostname resolves. DNS lives
       outside Azure (external registrar, propagation lag) so a miss is a WARN.

  Cross-platform: uses .NET (HttpClient, System.Net.Dns) instead of Windows-only
  cmdlets so the same script runs under Windows PowerShell 5.1 and pwsh 7 on the
  Linux CI runner. Read-only and idempotent - safe to re-run.
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'RequireApiHealth',
  Justification = 'Consumed inside the Test-ApiHealth nested function; the analyzer cannot resolve cross-scope use.')]
param(
  # Promote the API health probe from a warning to a hard failure. Defaults from
  # AI4IA_SMOKE_REQUIRE_API_HEALTH so the azd hook stays argument-free.
  [switch]$RequireApiHealth = ([string]::Equals($env:AI4IA_SMOKE_REQUIRE_API_HEALTH, 'true', [System.StringComparison]::OrdinalIgnoreCase))
)

$ErrorActionPreference = 'Stop'
# PS 5.1 can default to TLS 1.0; ARM requires 1.2+. No-op on pwsh 7.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { Write-Verbose "TLS 1.2 enforcement skipped (pwsh 7 already secure): $($_.Exception.Message)" }

# --- result tracking -------------------------------------------------------
$script:Results = [System.Collections.Generic.List[object]]::new()

function Add-Result {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][ValidateSet('PASS', 'FAIL', 'WARN', 'SKIP')][string]$Status,
    [string]$Detail = ''
  )
  $script:Results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail })
  $color = switch ($Status) { 'PASS' { 'Green' } 'FAIL' { 'Red' } 'WARN' { 'Yellow' } default { 'DarkGray' } }
  $suffix = if ($Detail) { " - $Detail" } else { '' }
  Write-Host ("  [{0}] {1}{2}" -f $Status, $Name, $suffix) -ForegroundColor $color
}

# --- environment / auth helpers -------------------------------------------
function Get-AzdEnvMap {
  # azd injects outputs as process env vars in hooks; `azd env get-values` is a
  # belt-and-braces fallback for running the script outside the hook.
  $map = @{}
  try {
    $raw = & azd env get-values 2>$null
    if ($LASTEXITCODE -eq 0 -and $raw) {
      foreach ($line in $raw) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
          $val = $matches[2].Trim()
          if ($val.Length -ge 2 -and $val.StartsWith('"') -and $val.EndsWith('"')) {
            $val = $val.Substring(1, $val.Length - 2)
          }
          $map[$matches[1]] = $val
        }
      }
    }
  } catch { Write-Verbose "azd env get-values unavailable; relying on process env: $($_.Exception.Message)" }
  return $map
}

function Get-EnvValue {
  param([Parameter(Mandatory)][string]$Name)
  $v = [Environment]::GetEnvironmentVariable($Name)
  if (-not [string]::IsNullOrWhiteSpace($v)) { return $v }
  if ($script:AzdEnv -and $script:AzdEnv.ContainsKey($Name)) { return $script:AzdEnv[$Name] }
  return $null
}

function Get-MgmtToken {
  # azd is the credential postprovision always has (the deploy workflow runs only
  # `azd auth login`). Prefer it; fall back to az for local/dev convenience.
  try {
    $t = & azd auth token --scope 'https://management.azure.com/.default' --output json 2>$null | ConvertFrom-Json
    if ($t -and $t.token) { return $t.token }
  } catch { Write-Verbose "azd auth token unavailable; falling back to az: $($_.Exception.Message)" }
  try {
    $t = & az account get-access-token --resource 'https://management.azure.com' --output json 2>$null | ConvertFrom-Json
    if ($t -and $t.accessToken) { return $t.accessToken }
  } catch { Write-Verbose "az access-token unavailable: $($_.Exception.Message)" }
  return $null
}

function Invoke-HttpProbe {
  param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 10)
  try { Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue } catch { Write-Verbose "System.Net.Http already available: $($_.Exception.Message)" }
  $client = [System.Net.Http.HttpClient]::new()
  try {
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $resp = $client.GetAsync($Url).GetAwaiter().GetResult()
    $body = ''
    try { $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult() } catch { Write-Verbose "could not read probe response body: $($_.Exception.Message)" }
    return [pscustomobject]@{ Ok = $resp.IsSuccessStatusCode; Status = [int]$resp.StatusCode; Body = $body; Error = $null }
  } catch {
    $ex = $_.Exception
    while ($ex.InnerException) { $ex = $ex.InnerException }
    return [pscustomobject]@{ Ok = $false; Status = 0; Body = ''; Error = $ex.Message }
  } finally {
    $client.Dispose()
  }
}

# --- checks ----------------------------------------------------------------
function Test-ModelDeployment {
  # HARD GATE. Foundry accounts + their model deployments are created by provision,
  # so they MUST exist and be Succeeded right now. Read account names straight from
  # the AZURE_FOUNDRY_ENDPOINTS output (never hard-code resource names).
  $foundryRaw = Get-EnvValue 'AZURE_FOUNDRY_ENDPOINTS'
  $subId = Get-EnvValue 'AZURE_SUBSCRIPTION_ID'
  $rg = Get-EnvValue 'AZURE_RESOURCE_GROUP'
  if ([string]::IsNullOrWhiteSpace($foundryRaw)) {
    Add-Result -Name 'model-deployments' -Status 'SKIP' -Detail 'AZURE_FOUNDRY_ENDPOINTS not set'
    return
  }
  $accounts = @()
  try {
    $accounts = @(($foundryRaw | ConvertFrom-Json) | ForEach-Object { $_.accountName } | Where-Object { $_ })
  } catch {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail "could not parse AZURE_FOUNDRY_ENDPOINTS: $($_.Exception.Message)"
    return
  }
  if ($accounts.Count -eq 0) {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail 'no Foundry accounts in AZURE_FOUNDRY_ENDPOINTS'
    return
  }
  if ([string]::IsNullOrWhiteSpace($subId) -or [string]::IsNullOrWhiteSpace($rg)) {
    Add-Result -Name 'model-deployments' -Status 'SKIP' -Detail 'AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP not set'
    return
  }
  $token = Get-MgmtToken
  if (-not $token) {
    Add-Result -Name 'model-deployments' -Status 'WARN' -Detail 'no ARM token (azd/az auth) - cannot verify deployments'
    return
  }
  $headers = @{ Authorization = "Bearer $token" }
  foreach ($account in $accounts) {
    $uri = "https://management.azure.com/subscriptions/$subId/resourceGroups/$rg/providers/Microsoft.CognitiveServices/accounts/$account/deployments?api-version=2023-05-01"
    try {
      $resp = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec 30
    } catch {
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail "ARM query failed: $($_.Exception.Message)"
      continue
    }
    $deployments = @($resp.value)
    if ($deployments.Count -eq 0) {
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail 'account has zero model deployments'
      continue
    }
    $bad = @($deployments | Where-Object { "$($_.properties.provisioningState)" -ne 'Succeeded' })
    if ($bad.Count -gt 0) {
      $names = ($bad | ForEach-Object { "$($_.name)=$($_.properties.provisioningState)" }) -join ', '
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail "not Succeeded: $names"
      continue
    }
    Add-Result -Name "model-deployments/$account" -Status 'PASS' -Detail "$($deployments.Count) deployment(s) Succeeded"
  }
}

function Test-ApiHealth {
  $base = Get-EnvValue 'AZURE_API_URL'
  if ([string]::IsNullOrWhiteSpace($base)) {
    Add-Result -Name 'api-health' -Status 'SKIP' -Detail 'AZURE_API_URL not set'
    return
  }
  $base = $base.TrimEnd('/')
  $failures = @()
  foreach ($path in @('/health/live', '/health/ready')) {
    $url = "$base$path"
    $result = $null
    # api may be cold-starting (or still the pre-deploy placeholder image); retry a
    # few times with short backoff. Kept brief because this is best-effort - the
    # real hard gate is the model-deployments check, not api liveness.
    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
      $result = Invoke-HttpProbe -Url $url -TimeoutSec 8
      if ($result.Ok) { break }
      if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds ([Math]::Min(4 * $attempt, 8)) }
    }
    if (-not $result.Ok) {
      $detail = if ($result.Status -gt 0) { "HTTP $($result.Status)" } else { $result.Error }
      $failures += "$path ($detail)"
    }
  }
  if ($failures.Count -eq 0) {
    Add-Result -Name 'api-health' -Status 'PASS' -Detail "$base/health/{live,ready} -> 200"
    return
  }
  $msg = "unreachable: $($failures -join ', ')"
  if ($RequireApiHealth) {
    Add-Result -Name 'api-health' -Status 'FAIL' -Detail $msg
  } else {
    Add-Result -Name 'api-health' -Status 'WARN' -Detail "$msg (runs pre-deploy; set AI4IA_SMOKE_REQUIRE_API_HEALTH=true to enforce)"
  }
}

function Test-CustomDomainDns {
  [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '',
    Justification = 'Dns is an acronym, not a plural noun; renaming would obscure intent.')]
  param()
  $domains = @(
    @{ Var = 'AI4IA_WEB_CUSTOM_DOMAIN'; Value = (Get-EnvValue 'AI4IA_WEB_CUSTOM_DOMAIN') }
    @{ Var = 'AI4IA_PROXY_CUSTOM_DOMAIN'; Value = (Get-EnvValue 'AI4IA_PROXY_CUSTOM_DOMAIN') }
  ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_.Value) }
  if ($domains.Count -eq 0) {
    Add-Result -Name 'custom-domain-dns' -Status 'SKIP' -Detail 'no custom-domain vars set'
    return
  }
  foreach ($d in $domains) {
    try {
      $addrs = [System.Net.Dns]::GetHostAddresses($d.Value)
      if ($addrs -and $addrs.Count -gt 0) {
        Add-Result -Name "custom-domain-dns/$($d.Value)" -Status 'PASS' -Detail "resolves -> $($addrs[0].IPAddressToString)"
      } else {
        Add-Result -Name "custom-domain-dns/$($d.Value)" -Status 'WARN' -Detail 'no A/AAAA records (DNS may be propagating)'
      }
    } catch {
      Add-Result -Name "custom-domain-dns/$($d.Value)" -Status 'WARN' -Detail "does not resolve yet: $($_.Exception.Message)"
    }
  }
}

# --- main ------------------------------------------------------------------
Write-Host '== AI4IA postprovision smoke tests ==' -ForegroundColor Cyan
$script:AzdEnv = Get-AzdEnvMap

$checks = @(
  @{ Label = 'Model deployments (hard gate)'; Fn = { Test-ModelDeployment } }
  @{ Label = 'API health'; Fn = { Test-ApiHealth } }
  @{ Label = 'Custom-domain DNS'; Fn = { Test-CustomDomainDns } }
)
foreach ($check in $checks) {
  Write-Host ("{0}:" -f $check.Label)
  try {
    & $check.Fn
  } catch {
    # An unexpected error inside a check is itself a failure signal.
    Add-Result -Name $check.Label -Status 'FAIL' -Detail "unexpected error: $($_.Exception.Message)"
  }
}

$pass = @($script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
$warn = @($script:Results | Where-Object { $_.Status -eq 'WARN' }).Count
$skip = @($script:Results | Where-Object { $_.Status -eq 'SKIP' }).Count
$fail = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count

Write-Host ''
Write-Host ("== Summary: {0} PASS, {1} WARN, {2} SKIP, {3} FAIL ==" -f $pass, $warn, $skip, $fail) -ForegroundColor Cyan

if ($fail -gt 0) {
  Write-Host 'Smoke tests FAILED: a provisioned resource that should exist is missing or unhealthy.' -ForegroundColor Red
  exit 1
}
Write-Host 'Smoke tests passed.' -ForegroundColor Green
exit 0
