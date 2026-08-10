#requires -Version 5.1
<#
.SYNOPSIS
  Post-provision smoke tests for AI4IA. Fails LOUDLY (non-zero exit) when a
  resource that SHOULD exist after `azd provision` is missing or unhealthy, so a
  broken deploy can no longer look successful.

.DESCRIPTION
  Runs in the azd `postprovision` hook (see azure.yaml). azd injects every infra
  output into this process's environment (AZURE_API_URL, AZURE_RESOURCE_GROUP,
  AZURE_SUBSCRIPTION_ID, AZURE_EXPECTED_MODEL_DEPLOYMENTS, ...). Values are read from the
  environment first and fall back to `azd env get-values`.

  Checks (each conditional on the relevant resource/var actually existing -
  fail-closed only when the resource SHOULD exist):

    1. Model deployments (HARD GATE). The Foundry/OpenAI accounts and their model
       deployments are created by `azd provision` itself, so immediately after
       provision they MUST exist and report provisioningState == 'Succeeded'.
       Queried via the ARM REST API with a token from `azd auth token` (falls back
       to `az account get-access-token`). Missing outputs or credentials are FAIL:
       an unevaluated provision prerequisite is not a successful provision.

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

    4. Gateway topology outputs (HARD GATE). The normal model URL must be the
       proxy /openai URL, while realtime must be the APIM /openai URL. This catches
       reversed or looped module wiring before an application image is deployed.

    5. App Configuration sentinel (HARD GATE). Reconciles Warm:Sentinel through
       the signed-in deployment identity after ARM role creation, with bounded
       retries for data-plane RBAC propagation.

    6. Content Understanding defaults (HARD GATE WHEN ENABLED). Consumes explicit
       primary account/region/endpoint/deployment outputs from Bicep and PATCHes
       the documented resource defaults. Missing outputs/token or exhausted RBAC
       retries fail the provision. Disabled CU remains an explicit SKIP.

  Cross-platform: uses .NET (HttpClient, System.Net.Dns) instead of Windows-only
  cmdlets so the same script runs under Windows PowerShell 5.1 and pwsh 7 on the
  Linux CI runner. Idempotent and safe to re-run; its only writes are the documented
  App Configuration sentinel and Content Understanding defaults reconciliation.
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
  param([ValidateRange(1, 300)][int]$TimeoutSec = 60)

  # azd is the credential postprovision always has (the deploy workflow runs only
  # `azd auth login`). Prefer it; fall back to az for local/dev convenience. Both
  # commands share one timeout budget so a credential process cannot hang the gate.
  $startedAt = Get-MonotonicTime
  $result = Invoke-NativeWithTimeout -Command 'azd' -Arguments @(
    'auth', 'token', '--scope', 'https://management.azure.com/.default'
  ) -TimeoutSec $TimeoutSec
  if ($result.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($result.Output)) {
    return $result.Output.Trim()
  }
  $remaining = $TimeoutSec - ((Get-MonotonicTime) - $startedAt)
  if ($remaining -lt 1) { return $null }
  $fallbackTimeout = [Math]::Max(1, [Math]::Floor($remaining))
  $result = Invoke-NativeWithTimeout -Command 'az' -Arguments @(
    'account', 'get-access-token',
    '--resource', 'https://management.azure.com',
    '--query', 'accessToken',
    '--output', 'tsv'
  ) -TimeoutSec $fallbackTimeout
  if ($result.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($result.Output)) {
    return $result.Output.Trim()
  }
  return $null
}

function Get-MonotonicTime {
  return [System.Diagnostics.Stopwatch]::GetTimestamp() / [double][System.Diagnostics.Stopwatch]::Frequency
}

function Invoke-NativeWithTimeout {
  param(
    [Parameter(Mandatory)][string]$Command,
    [Parameter(Mandatory)][string[]]$Arguments,
    [Parameter(Mandatory)][ValidateRange(1, 300)][int]$TimeoutSec
  )

  # PowerShell's native invocation has no timeout. Run Azure CLIs in a child job
  # so a stalled auth/data-plane command cannot escape a provisioning deadline.
  $jobArguments = @($Command) + @($Arguments)
  $job = Start-Job -ScriptBlock {
    $nativeCommand = [string]$args[0]
    $nativeArguments = @($args | Select-Object -Skip 1)
    $exitCode = 1
    $output = @()
    try {
      $output = @(& $nativeCommand @nativeArguments 2>$null)
      if ($LASTEXITCODE -is [int]) { $exitCode = $LASTEXITCODE }
    } catch {
      # The caller treats every nonzero result identically and retries without
      # emitting CLI diagnostics or arguments.
      Write-Verbose 'Bounded native Azure command failed.'
    }
    return [pscustomobject]@{
      ExitCode = [int]$exitCode
      Output = ($output -join "`n")
    }
  } -ArgumentList $jobArguments
  try {
    $completed = Wait-Job -Job $job -Timeout $TimeoutSec
    if (-not $completed) {
      Stop-Job -Job $job
      return [pscustomobject]@{ ExitCode = 124; Output = ''; TimedOut = $true }
    }
    $result = @(Receive-Job -Job $job)
    if ($result.Count -ne 1) {
      return [pscustomobject]@{ ExitCode = 1; Output = ''; TimedOut = $false }
    }
    return [pscustomobject]@{
      ExitCode = [int]$result[0].ExitCode
      Output = [string]$result[0].Output
      TimedOut = $false
    }
  } finally {
    Remove-Job -Job $job -Force
  }
}

function Get-CognitiveServicesToken {
  param([Parameter(Mandatory)][ValidateRange(1, 300)][int]$TimeoutSec)

  # Content Understanding accepts Microsoft Entra tokens for this documented
  # scope. Prefer azd because every lifecycle hook has that credential; Azure CLI
  # remains the local/CI fallback and is never asked for an account key. Both
  # commands share one timeout budget.
  $startedAt = Get-MonotonicTime
  $result = Invoke-NativeWithTimeout -Command 'azd' -Arguments @(
    'auth', 'token', '--scope', 'https://cognitiveservices.azure.com/.default'
  ) -TimeoutSec $TimeoutSec
  if ($result.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($result.Output)) {
    return $result.Output.Trim()
  }

  $remaining = $TimeoutSec - ((Get-MonotonicTime) - $startedAt)
  if ($remaining -lt 1) { return $null }
  $fallbackTimeout = [Math]::Max(1, [Math]::Floor($remaining))
  $result = Invoke-NativeWithTimeout -Command 'az' -Arguments @(
    'account', 'get-access-token',
    '--resource', 'https://cognitiveservices.azure.com',
    '--query', 'accessToken',
    '--output', 'tsv'
  ) -TimeoutSec $fallbackTimeout
  if ($result.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($result.Output)) {
    return $result.Output.Trim()
  }
  return $null
}

function Invoke-AppConfigSet {
  param(
    [Parameter(Mandatory)][string[]]$Arguments,
    [Parameter(Mandatory)][ValidateRange(1, 300)][int]$TimeoutSec
  )
  $result = Invoke-NativeWithTimeout -Command 'az' -Arguments $Arguments -TimeoutSec $TimeoutSec
  return [int]$result.ExitCode
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
  # so every exact catalog-driven deployment MUST exist and be Succeeded right now.
  # Consume the Bicep output verbatim; never reconstruct account/deployment names.
  $expectedRaw = Get-EnvValue 'AZURE_EXPECTED_MODEL_DEPLOYMENTS'
  $subId = Get-EnvValue 'AZURE_SUBSCRIPTION_ID'
  $rg = Get-EnvValue 'AZURE_RESOURCE_GROUP'
  if ([string]::IsNullOrWhiteSpace($expectedRaw)) {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail 'required output AZURE_EXPECTED_MODEL_DEPLOYMENTS not set'
    return
  }
  $targets = @()
  try {
    $targets = @(($expectedRaw | ConvertFrom-Json))
  } catch {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail "could not parse AZURE_EXPECTED_MODEL_DEPLOYMENTS: $($_.Exception.Message)"
    return
  }
  if ($targets.Count -eq 0) {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail 'AZURE_EXPECTED_MODEL_DEPLOYMENTS contains no account records'
    return
  }
  if ([string]::IsNullOrWhiteSpace($subId) -or [string]::IsNullOrWhiteSpace($rg)) {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail 'required output(s) AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP not set'
    return
  }
  $token = Get-MgmtToken
  if (-not $token) {
    Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail 'no ARM token from azd or Azure CLI; cannot verify required deployments'
    return
  }
  $headers = @{ Authorization = "Bearer $token" }
  foreach ($target in $targets) {
    $account = "$($target.accountName)".Trim()
    $expectedNames = @($target.deploymentNames | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    if ([string]::IsNullOrWhiteSpace($account) -or $expectedNames.Count -eq 0) {
      Add-Result -Name 'model-deployments' -Status 'FAIL' -Detail 'expected deployment record is missing accountName or deploymentNames'
      continue
    }
    $uri = "https://management.azure.com/subscriptions/$subId/resourceGroups/$rg/providers/Microsoft.CognitiveServices/accounts/$account/deployments?api-version=2023-05-01"
    try {
      $resp = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec 30
    } catch {
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail "ARM query failed: $($_.Exception.Message)"
      continue
    }
    $deployments = @($resp.value)
    $byName = @{}
    foreach ($deployment in $deployments) {
      $name = "$($deployment.name)".Trim()
      if (-not [string]::IsNullOrWhiteSpace($name)) {
        $byName[$name.ToLowerInvariant()] = $deployment
      }
    }
    $missing = @($expectedNames | Where-Object { -not $byName.ContainsKey($_.ToLowerInvariant()) })
    $expectedKeys = @($expectedNames | ForEach-Object { $_.ToLowerInvariant() })
    $unexpected = @(
      $deployments |
        Where-Object {
          $name = "$($_.name)".Trim()
          -not [string]::IsNullOrWhiteSpace($name) -and $expectedKeys -notcontains $name.ToLowerInvariant()
        } |
        ForEach-Object { "$($_.name)" }
    )
    $bad = @(
      $expectedNames |
        Where-Object { $byName.ContainsKey($_.ToLowerInvariant()) } |
        ForEach-Object { $byName[$_.ToLowerInvariant()] } |
        Where-Object { "$($_.properties.provisioningState)" -ne 'Succeeded' }
    )
    if ($missing.Count -gt 0) {
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail "missing expected deployment(s): $($missing -join ', ')"
      continue
    }
    if ($unexpected.Count -gt 0) {
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail "unexpected stale deployment(s): $($unexpected -join ', ')"
      continue
    }
    if ($bad.Count -gt 0) {
      $names = ($bad | ForEach-Object { "$($_.name)=$($_.properties.provisioningState)" }) -join ', '
      Add-Result -Name "model-deployments/$account" -Status 'FAIL' -Detail "not Succeeded: $names"
      continue
    }
    Add-Result -Name "model-deployments/$account" -Status 'PASS' -Detail "$($expectedNames.Count) expected deployment(s) Succeeded"
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

function Test-GatewayTopology {
    $proxyUrl = Get-EnvValue 'AZURE_PROXY_URL'
    $modelUrl = Get-EnvValue 'AZURE_MODEL_GATEWAY_URL'
    $apimUrl = Get-EnvValue 'AZURE_APIM_GATEWAY_URL'
    $realtimeUrl = Get-EnvValue 'AZURE_REALTIME_GATEWAY_URL'

    $missing = @(
      @{ Name = 'AZURE_PROXY_URL'; Value = $proxyUrl }
      @{ Name = 'AZURE_MODEL_GATEWAY_URL'; Value = $modelUrl }
      @{ Name = 'AZURE_APIM_GATEWAY_URL'; Value = $apimUrl }
      @{ Name = 'AZURE_REALTIME_GATEWAY_URL'; Value = $realtimeUrl }
    ) | Where-Object { [string]::IsNullOrWhiteSpace($_.Value) }
    if ($missing.Count -gt 0) {
      Add-Result -Name 'gateway-topology' -Status 'FAIL' -Detail "missing output(s): $((@($missing.Name) -join ', '))"
      return
    }

    $expectedModel = "$($proxyUrl.TrimEnd('/'))/openai"
    $expectedRealtime = "$($apimUrl.TrimEnd('/'))/openai"
    $modelMatches = [string]::Equals($modelUrl.TrimEnd('/'), $expectedModel, [System.StringComparison]::OrdinalIgnoreCase)
    $realtimeMatches = [string]::Equals($realtimeUrl.TrimEnd('/'), $expectedRealtime, [System.StringComparison]::OrdinalIgnoreCase)
    $pathsAreSplit = -not [string]::Equals($modelUrl.TrimEnd('/'), $realtimeUrl.TrimEnd('/'), [System.StringComparison]::OrdinalIgnoreCase)

    if ($modelMatches -and $realtimeMatches -and $pathsAreSplit) {
      Add-Result -Name 'gateway-topology' -Status 'PASS' -Detail 'HTTP/SSE=proxy -> APIM; realtime=APIM'
      return
    }

    Add-Result -Name 'gateway-topology' -Status 'FAIL' -Detail "expected model=$expectedModel realtime=$expectedRealtime; got model=$modelUrl realtime=$realtimeUrl"
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

function Register-AppConfigurationSentinel {
  # Do not deploy keyValues as ARM children: a store with local authentication
  # disabled requires ARM pass-through mode, and a role created in that same
  # deployment may not have propagated to the data plane yet. Reconcile after
  # provision through the already signed-in deployment identity instead.
  $endpoint = Get-EnvValue 'AZURE_APP_CONFIG_ENDPOINT'
  if ([string]::IsNullOrWhiteSpace($endpoint)) {
    Add-Result -Name 'App Configuration sentinel' -Status 'FAIL' -Detail 'required output AZURE_APP_CONFIG_ENDPOINT not set'
    return
  }

  # The narrow Data Owner role is granted to the OIDC deployment principal.
  # A workstation user running a break-glass local provision is a different
  # identity; do not wait 15 minutes on a role it was never granted. Greenfield
  # setup is workflow-only, and an existing sentinel survives local provisions.
  $provisionerPrincipalId = Get-EnvValue 'AZURE_PRINCIPAL_ID'
  if ([string]::IsNullOrWhiteSpace($provisionerPrincipalId)) {
    Add-Result -Name 'App Configuration sentinel' -Status 'SKIP' -Detail 'AZURE_PRINCIPAL_ID not set; workflow-owned sentinel left unchanged'
    return
  }

  $label = Get-EnvValue 'AZURE_APP_CONFIG_LABEL'
  if ([string]::IsNullOrWhiteSpace($label)) {
    # Backward-compatible fallback for an existing azd environment that predates
    # the explicit Bicep output. Keep an empty value truly unlabeled.
    $label = Get-EnvValue 'AI4IA_PROXY_APPCONFIG_LABEL'
  }

  $arguments = @(
    'appconfig', 'kv', 'set',
    '--endpoint', $endpoint,
    '--key', 'Warm:Sentinel',
    '--value', 'ready',
    '--auth-mode', 'login',
    '--yes',
    '--output', 'none'
  )
  if (-not [string]::IsNullOrWhiteSpace($label)) {
    $arguments += @('--label', $label)
  }

  # Azure documents that a new data-plane role assignment can take up to
  # 15 minutes to propagate. Ordinary deploys complete on the first attempt;
  # greenfield and role-repair deploys must wait out the documented window
  # rather than publishing an empty store as a healthy warm-refresh plane.
  $budgetSeconds = 900
  $startedAt = Get-MonotonicTime
  $attempt = 0
  $retrySeconds = 30
  while ($true) {
    $remaining = $budgetSeconds - ((Get-MonotonicTime) - $startedAt)
    if ($remaining -lt 1) { break }
    $attempt++
    $commandTimeout = [Math]::Max(1, [Math]::Min(60, [Math]::Floor($remaining)))
    try {
      $exitCode = Invoke-AppConfigSet -Arguments $arguments -TimeoutSec $commandTimeout
      if ($exitCode -eq 0) {
        $scope = if ([string]::IsNullOrWhiteSpace($label)) { 'unlabeled' } else { 'configured label' }
        Add-Result -Name 'App Configuration sentinel' -Status 'PASS' -Detail "Warm:Sentinel=ready ($scope)"
        return
      }
    } catch {
      # Retry below. Deliberately do not echo the exception or command arguments.
      Write-Verbose 'App Configuration data-plane set attempt failed; retrying without emitting CLI details.'
    }
    $remaining = $budgetSeconds - ((Get-MonotonicTime) - $startedAt)
    if ($remaining -lt 1) { break }
    $sleepSeconds = [Math]::Min($retrySeconds, [Math]::Floor($remaining))
    if ($sleepSeconds -gt 0) { Start-Sleep -Seconds $sleepSeconds }
  }

  # Unlike optional context, this store is always wired into the proxy. Failing
  # after the full RBAC window means the deployed configuration plane is not the
  # one the template claims, so fail the provision instead of shipping a false
  # healthy state.
  Add-Result -Name 'App Configuration sentinel' -Status 'FAIL' -Detail "Entra-authenticated set failed within the ${budgetSeconds}-second budget after $attempt attempt(s)"
}

function Register-ContentUnderstandingDefault {
  # Content Understanding will not run an analyzer until the resource has a
  # `modelDeployments` default mapping. Without it every analyze job returns
  # `status=Failed` with innererror `ResourceError`, and nothing in Bicep can
  # set it: it is a data-plane PATCH on the account, not an ARM property.
  #
  # Bicep emits the selected primary account, region, endpoint, and exact model
  # deployment names. Never choose a region here and never rebuild names from a
  # convention: both drift silently when AZURE_LOCATION or catalog naming changes.
  try {
    $enabledRaw = Get-EnvValue 'AZURE_CONTENT_UNDERSTANDING_ENABLED'
    if ([string]::IsNullOrWhiteSpace($enabledRaw)) {
      Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail 'required output AZURE_CONTENT_UNDERSTANDING_ENABLED not set'
      return
    }
    if ([string]::Equals($enabledRaw, 'false', [System.StringComparison]::OrdinalIgnoreCase)) {
      Add-Result -Name 'Content Understanding defaults' -Status 'SKIP' -Detail 'disabled by AZURE_CONTENT_UNDERSTANDING_ENABLED=false'
      return
    }
    if (-not [string]::Equals($enabledRaw, 'true', [System.StringComparison]::OrdinalIgnoreCase)) {
      Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail "AZURE_CONTENT_UNDERSTANDING_ENABLED must be true or false, got '$enabledRaw'"
      return
    }

    $required = @(
      @{ Name = 'AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME'; Value = (Get-EnvValue 'AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME') }
      @{ Name = 'AZURE_PRIMARY_FOUNDRY_REGION'; Value = (Get-EnvValue 'AZURE_PRIMARY_FOUNDRY_REGION') }
      @{ Name = 'AZURE_PRIMARY_FOUNDRY_ENDPOINT'; Value = (Get-EnvValue 'AZURE_PRIMARY_FOUNDRY_ENDPOINT') }
      @{ Name = 'AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT'; Value = (Get-EnvValue 'AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT') }
      @{ Name = 'AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT'; Value = (Get-EnvValue 'AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT') }
    )
    $missing = @($required | Where-Object { [string]::IsNullOrWhiteSpace($_.Value) })
    if ($missing.Count -gt 0) {
      Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail "missing required output(s): $((@($missing.Name) -join ', '))"
      return
    }

    $account = ($required | Where-Object Name -eq 'AZURE_PRIMARY_FOUNDRY_ACCOUNT_NAME').Value
    $region = ($required | Where-Object Name -eq 'AZURE_PRIMARY_FOUNDRY_REGION').Value
    $endpoint = ($required | Where-Object Name -eq 'AZURE_PRIMARY_FOUNDRY_ENDPOINT').Value
    $completion = ($required | Where-Object Name -eq 'AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT').Value
    $embedding = ($required | Where-Object Name -eq 'AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT').Value

    $parsedEndpoint = [uri]$endpoint
    if (-not $parsedEndpoint.IsAbsoluteUri -or $parsedEndpoint.Scheme -ne 'https') {
      Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail 'AZURE_PRIMARY_FOUNDRY_ENDPOINT must be an absolute https URL'
      return
    }

    $budgetSeconds = 900
    $startedAt = Get-MonotonicTime
    $remaining = $budgetSeconds - ((Get-MonotonicTime) - $startedAt)
    $tokenTimeout = [Math]::Max(1, [Math]::Min(60, [Math]::Floor($remaining)))
    $token = Get-CognitiveServicesToken -TimeoutSec $tokenTimeout
    if ([string]::IsNullOrWhiteSpace($token)) {
      Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail 'no Cognitive Services token from azd or Azure CLI'
      return
    }

    $body = @{ modelDeployments = @{
        'prebuilt-analyzer-completion-mini' = $completion
        'prebuilt-analyzer-completion'      = $completion
        'prebuilt-analyzer-embedding'       = $embedding
      } } | ConvertTo-Json -Depth 5
      $base = $endpoint.TrimEnd('/')
      $lastError = $null
      $attempt = 0
    $retrySeconds = 30
    while ($true) {
      $remaining = $budgetSeconds - ((Get-MonotonicTime) - $startedAt)
      if ($remaining -lt 1) { break }
      $attempt++
      if ($attempt -gt 1) {
        $tokenTimeout = [Math]::Max(1, [Math]::Min(60, [Math]::Floor($remaining)))
        $refreshedToken = Get-CognitiveServicesToken -TimeoutSec $tokenTimeout
        if (-not [string]::IsNullOrWhiteSpace($refreshedToken)) {
          $token = $refreshedToken
        }
      }
      $remaining = $budgetSeconds - ((Get-MonotonicTime) - $startedAt)
      if ($remaining -lt 1) { break }
      $requestTimeout = [Math]::Max(1, [Math]::Min(60, [Math]::Floor($remaining)))
      try {
        Invoke-RestMethod -Method Patch -Uri "$base/contentunderstanding/defaults?api-version=2025-11-01" `
          -Headers @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/merge-patch+json' } -Body $body -TimeoutSec $requestTimeout | Out-Null
        Add-Result -Name 'Content Understanding defaults' -Status 'PASS' -Detail "account=$account region=$region completion=$completion"
        return
      } catch {
        $lastError = $_.Exception.Message
        # Azure RBAC documents up to 10 minutes for role changes; keep a
        # 15-minute budget for the data-plane check and refresh the token above.
        $remaining = $budgetSeconds - ((Get-MonotonicTime) - $startedAt)
        if ($remaining -lt 1) { break }
        $sleepSeconds = [Math]::Min($retrySeconds, [Math]::Floor($remaining))
        if ($sleepSeconds -gt 0) { Start-Sleep -Seconds $sleepSeconds }
      }
    }
    Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail "PATCH failed within the ${budgetSeconds}-second budget after $attempt attempt(s): $lastError"
  } catch {
    Add-Result -Name 'Content Understanding defaults' -Status 'FAIL' -Detail "PATCH prerequisite/setup failed: $($_.Exception.Message)"
  }
}

$checks = @(
  @{ Label = 'Model deployments (hard gate)'; Fn = { Test-ModelDeployment } }
  @{ Label = 'API health'; Fn = { Test-ApiHealth } }
  @{ Label = 'Custom-domain DNS'; Fn = { Test-CustomDomainDns } }
  @{ Label = 'Gateway topology outputs (hard gate)'; Fn = { Test-GatewayTopology } }
  @{ Label = 'App Configuration sentinel'; Fn = { Register-AppConfigurationSentinel } }
  @{ Label = 'Content Understanding defaults'; Fn = { Register-ContentUnderstandingDefault } }
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
