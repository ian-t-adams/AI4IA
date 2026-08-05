<#
.SYNOPSIS
  Capture the facts you cannot reconstruct after an AI4IA resource group is deleted.

.DESCRIPTION
  Read-only. `inventory.ps1` snapshots the *infrastructure* (Foundry accounts, model
  deployments, vault names) so `azd provision` can rebuild it. It captures nothing
  about the data, and the data is the part with no IaC to rebuild it from.

  This script records the three things that become unrecoverable, or unfindable, the
  moment the resource group goes away:

  1. **Cosmos restore coordinates.** Continuous backup restores to a *new* account and
     is addressed by the restorable-account instance id, the location, and a timestamp
     inside the retention window. After deletion you cannot look those up from the
     deleted account -- so they are captured here, before.
  2. **Blob inventory.** Uploaded documents and generated images/videos have no restore
     path at all. This lists what is there and how large it is, so "export anything you
     need to keep" is a decision made against a manifest rather than from memory.
  3. **Key Vault secret names** (names only -- never values). Purged secrets are gone;
     users must re-enter them. Knowing which users held which secret names is the
     difference between a targeted notice and a broadcast apology.

  Every probe is independent and failure-tolerant: a permission gap on one resource is
  recorded in the manifest and does not abort the capture. That matters because this
  runs immediately before a destructive step, where a crash halfway through is worse
  than a partial manifest that says which parts are partial.

.EXAMPLE
  ./scripts/capture-data-recovery-state.ps1 -Subscription <id> -ResourceGroup rg-ai4ia-<env>

.EXAMPLE
  # Faster on large accounts: record containers without enumerating every blob.
  ./scripts/capture-data-recovery-state.ps1 -Subscription <id> -ResourceGroup rg-ai4ia-<env> -SkipBlobSizes
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string] $Subscription,
    [Parameter(Mandatory)] [ValidateNotNullOrEmpty()] [string] $ResourceGroup,
    [string] $OutDir = "./.inventory",
    [switch] $SkipBlobSizes
)

$ErrorActionPreference = "Stop"

# Containers the application treats as scratch. Recorded, but not counted as data
# loss, so the summary stays honest about what actually matters.
$DisposableContainers = @("ephemeral-attachments")

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$capturedUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$dir = Join-Path $OutDir $stamp
New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "Capturing data-recovery state for $ResourceGroup -> $dir" -ForegroundColor Cyan
az account set --subscription $Subscription | Out-Null

$warnings = [System.Collections.Generic.List[string]]::new()

function Invoke-AzJson {
    <#
      .SYNOPSIS
        Run an `az` query and return parsed JSON, or $null if it fails.
      .DESCRIPTION
        Permission gaps are expected here (listing secret metadata needs a role the
        operator may not hold), so a failure is recorded and execution continues.
    #>
    param([string[]] $AzArgs, [string] $What)
    try {
        $raw = & az @AzArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            $warnings.Add("${What}: az exited $LASTEXITCODE") | Out-Null
            Write-Warning "  ! $What - not captured (az exited $LASTEXITCODE)"
            return $null
        }
        if (-not $raw) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch {
        $warnings.Add("${What}: $($_.Exception.Message)") | Out-Null
        Write-Warning "  ! $What - not captured ($($_.Exception.Message))"
        return $null
    }
}

# --- 1. Cosmos: the canonical store -----------------------------------------
Write-Host "  - Cosmos restore coordinates"
$cosmosReport = @()
$cosmosAccounts = Invoke-AzJson @("cosmosdb", "list", "-g", $ResourceGroup, "-o", "json") "cosmosdb list"
foreach ($acct in @($cosmosAccounts)) {
    if (-not $acct) { continue }
    $policy = $acct.backupPolicy.type
    $tier = $acct.backupPolicy.continuousModeProperties.tier
    $restorable = Invoke-AzJson @(
        "cosmosdb", "restorable-database-account", "list",
        "--query", "[?accountName=='$($acct.name)'] | [0]", "-o", "json"
    ) "restorable-database-account for $($acct.name)"

    # ConvertFrom-Json turns the ISO string into a [datetime], which then renders in
    # the *operator's* locale ("8/2/2026 8:27:02 AM"). This value is a restore
    # argument that may be read months later, on another machine, by someone who
    # cannot tell 8/2 from 2/8. Normalize it back to unambiguous ISO-8601 UTC.
    $oldest = $restorable.oldestRestorableTime
    if ($oldest -is [datetime]) {
        $oldest = $oldest.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }

    $cosmosReport += [ordered]@{
        name = $acct.name
        location = $acct.location
        backupPolicy = $policy
        continuousTier = $tier
        # Continuous backup is the only mode that can be self-service restored.
        # Anything else means the restore path is a support ticket, or nothing.
        restorable = ($policy -eq "Continuous")
        # Restore addresses the *restorable account instance*, not the account name.
        # This id is not derivable once the account is deleted.
        restorableInstanceId = $restorable.name
        oldestRestorableTime = $oldest
        latestRestorableTime = $capturedUtc
    }
}

# --- 2. Blobs: no restore path ----------------------------------------------
Write-Host "  - Blob inventory"
$storageReport = @()
$storageAccounts = Invoke-AzJson @("storage", "account", "list", "-g", $ResourceGroup, "--query", "[].name", "-o", "json") "storage account list"
foreach ($sa in @($storageAccounts)) {
    if (-not $sa) { continue }
    $containers = Invoke-AzJson @(
        "storage", "container", "list", "--account-name", $sa,
        "--auth-mode", "login", "--query", "[].name", "-o", "json"
    ) "container list for $sa"

    $containerReport = @()
    foreach ($c in @($containers)) {
        if (-not $c) { continue }
        $count = $null
        $bytes = $null
        if (-not $SkipBlobSizes) {
            $lengths = Invoke-AzJson @(
                "storage", "blob", "list", "-c", $c, "--account-name", $sa,
                "--auth-mode", "login", "--query", "[].properties.contentLength", "-o", "json"
            ) "blob list for $sa/$c"
            if ($null -ne $lengths) {
                $measured = @($lengths) | Measure-Object -Sum
                $count = $measured.Count
                $bytes = [int64]($measured.Sum ?? 0)
            }
        }
        $containerReport += [ordered]@{
            name = $c
            blobCount = $count
            totalBytes = $bytes
            disposable = ($DisposableContainers -contains $c)
            # Stated per container rather than once in prose: there is no
            # point-in-time restore configured for blob here.
            restorable = $false
        }
    }
    $storageReport += [ordered]@{ account = $sa; containers = $containerReport }
}

# --- 3. Key Vault: purged by teardown ---------------------------------------
Write-Host "  - Key Vault secret names (names only)"
$vaultReport = @()
$vaults = Invoke-AzJson @("keyvault", "list", "-g", $ResourceGroup, "-o", "json") "keyvault list"
foreach ($v in @($vaults)) {
    if (-not $v) { continue }
    # Names only. A value read here would put a live credential in a file that is
    # explicitly meant to be copied out of the environment.
    $names = Invoke-AzJson @(
        "keyvault", "secret", "list", "--vault-name", $v.name, "--query", "[].name", "-o", "json"
    ) "secret names for $($v.name)"
    $vaultReport += [ordered]@{
        name = $v.name
        purgeProtection = [bool]$v.properties.enablePurgeProtection
        secretNamesCaptured = ($null -ne $names)
        secretNames = @($names)
    }
}

# --- Manifest ----------------------------------------------------------------
$liveBlobBytes = 0
$liveBlobCount = 0
# Tracked separately from the totals because a failed probe and an empty container
# both leave the counters at zero. Reporting "0 blobs" for a container we were not
# allowed to read would tell an operator there is nothing to lose, immediately
# before an irreversible delete. Absence of evidence is not evidence of absence.
$blobCountsIncomplete = $false
foreach ($s in $storageReport) {
    foreach ($c in $s.containers) {
        if ($c.disposable) { continue }
        if ($null -eq $c.totalBytes) { $blobCountsIncomplete = $true; continue }
        $liveBlobBytes += $c.totalBytes
        $liveBlobCount += $c.blobCount
    }
}

$manifest = [ordered]@{
    capturedUtc = $capturedUtc
    subscription = $Subscription
    resourceGroup = $ResourceGroup
    blobSizesSkipped = [bool]$SkipBlobSizes
    cosmos = $cosmosReport
    storage = $storageReport
    keyVaults = $vaultReport
    warnings = @($warnings)
}
$manifestPath = Join-Path $dir "data-recovery-state.json"
$manifest | ConvertTo-Json -Depth 8 | Out-File -Encoding utf8 $manifestPath

# --- Operator summary --------------------------------------------------------
Write-Host ""
Write-Host "== What survives teardown ==" -ForegroundColor Cyan
foreach ($c in $cosmosReport) {
    if ($c.restorable) {
        Write-Host ("  Cosmos {0}: restorable ({1}) - instance {2}, window {3} .. {4}" -f `
            $c.name, $c.continuousTier, $c.restorableInstanceId, $c.oldestRestorableTime, $c.latestRestorableTime) -ForegroundColor Green
    } else {
        Write-Host ("  Cosmos {0}: NOT self-service restorable (backup policy: {1})" -f $c.name, $c.backupPolicy) -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "== What does NOT survive teardown ==" -ForegroundColor Yellow
if ($SkipBlobSizes) {
    Write-Host "  Blob: sizes not measured (-SkipBlobSizes); containers listed in the manifest." -ForegroundColor Yellow
} elseif ($blobCountsIncomplete) {
    Write-Host "  Blob: SIZE UNKNOWN - one or more containers could not be read." -ForegroundColor Red
    Write-Host "        Listing blobs needs the data plane (Storage Blob Data Reader), not just" -ForegroundColor Red
    Write-Host "        control-plane rights. Do NOT read this as 'there is nothing to lose'." -ForegroundColor Red
    if ($liveBlobCount -gt 0) {
        Write-Host ("        Partial total from the containers that were readable: {0} blobs / {1:N1} MiB." -f `
            $liveBlobCount, ($liveBlobBytes / 1MB)) -ForegroundColor Red
    }
} else {
    Write-Host ("  Blob: {0} blobs / {1:N1} MiB across non-disposable containers - no restore path." -f `
        $liveBlobCount, ($liveBlobBytes / 1MB)) -ForegroundColor Yellow
}
foreach ($v in $vaultReport) {
    if ($v.secretNamesCaptured) {
        Write-Host ("  Key Vault {0}: {1} secret(s) will be purged and are unrecoverable." -f $v.name, $v.secretNames.Count) -ForegroundColor Yellow
    } else {
        Write-Host ("  Key Vault {0}: secret names NOT captured - you will not know who to notify." -f $v.name) -ForegroundColor Red
    }
}

if ($warnings.Count -gt 0) {
    Write-Host ""
    Write-Host "== Incomplete capture ==" -ForegroundColor Red
    foreach ($w in $warnings) { Write-Host "  - $w" -ForegroundColor Red }
    Write-Host "  Re-run with sufficient permissions, or accept these gaps knowingly." -ForegroundColor Red
}

Write-Host ""
Write-Host "Manifest: $manifestPath" -ForegroundColor Green
Write-Host "Copy this OUTSIDE the target resource group before running teardown.ps1." -ForegroundColor Yellow
