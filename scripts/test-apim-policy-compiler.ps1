[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$SubscriptionId,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ResourceGroup,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ServiceName
)

$ErrorActionPreference = 'Stop'
$apiVersion = '2024-05-01'
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
$apiName = "ai4ia-compiler-$suffix"
$apiPath = $apiName
$repoRoot = Split-Path -Parent $PSScriptRoot
$fragmentPayloadMaxBytes = 14 * 1024
$policyDocumentMaxBytes = 14 * 1024
$priorityPolicyPath = 'infra/policies/simplel7proxy-priority-retry.xml'
$realtimePolicyPath = 'infra/policies/realtime-routing.xml'
$managementBase = (
    "https://management.azure.com/subscriptions/$SubscriptionId" +
    "/resourceGroups/$ResourceGroup/providers/Microsoft.ApiManagement" +
    "/service/$ServiceName"
)
$apiUrl = "$managementBase/apis/$apiName`?api-version=$apiVersion"
$apiPolicyUrl = (
    "$managementBase/apis/$apiName/policies/policy" +
    "?api-version=$apiVersion"
)
$fragmentDefinitions = @(
    foreach ($index in 0..9) {
        @{
            ProductionId = "endpoint_selection_catalog_$($index)_33"
            TemporaryId = "ai4ia-compiler-catalog-$index-$suffix"
            Path = "infra/policies/simplel7proxy-endpoints-catalog-$index.xml"
        }
    }
    foreach ($index in 0..9) {
        @{
            ProductionId = "priority_policy_$($index)_33"
            TemporaryId = "ai4ia-compiler-priority-$index-$suffix"
            Path = "infra/policies/simplel7proxy-priority-fragment-$index.xml"
        }
    }
    @{
        ProductionId = 'endpoint_selection_setup_33'
        TemporaryId = "ai4ia-compiler-setup-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints.xml'
    }
)

function Assert-DiagnosticName {
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [string]$Pattern
    )

    if ($Name -notmatch $Pattern) {
        throw "Refusing to operate on non-diagnostic resource name '$Name'."
    }
}

function Assert-PolicyPayloadSize {
    param(
        [Parameter(Mandatory)]
        [string]$RelativePath,

        [Parameter(Mandatory)]
        [int]$MaximumBytes,

        [Parameter(Mandatory)]
        [string]$LimitName
    )

    $path = Join-Path $repoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required policy file does not exist: $RelativePath"
    }
    $rawBytes = (Get-Item -LiteralPath $path).Length
    if ($rawBytes -gt $MaximumBytes) {
        throw (
            "$RelativePath is $rawBytes raw UTF-8 bytes; " +
            "$LimitName is $MaximumBytes bytes."
        )
    }
    Write-Information (
        "LOCAL_POLICY_SIZE=$RelativePath`:$rawBytes/$MaximumBytes"
    ) -InformationAction Continue
}

function Invoke-ArmRequest {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('Get', 'Put', 'Delete')]
        [string]$Method,

        [Parameter(Mandatory)]
        [string]$Url,

        [Parameter(Mandatory)]
        [hashtable]$Headers,

        [hashtable]$Payload
    )

    try {
        $parameters = @{
            Method = $Method
            Uri = $Url
            Headers = $Headers
        }
        if ($null -ne $Payload) {
            $parameters.ContentType = 'application/json'
            $parameters.Body = $Payload | ConvertTo-Json -Depth 12 -Compress
        }
        $response = Invoke-WebRequest @parameters
        return [pscustomobject]@{
            Success = $true
            Status = [int]$response.StatusCode
            Body = $response.Content
            Headers = $response.Headers
        }
    }
    catch {
        $status = if ($_.Exception.Response) {
            [int]$_.Exception.Response.StatusCode
        }
        else {
            0
        }
        $body = $_.ErrorDetails.Message
        if ([string]::IsNullOrWhiteSpace($body)) {
            $body = $_.Exception.Message
        }
        return [pscustomobject]@{
            Success = $false
            Status = $status
            Body = $body
            Headers = @{}
        }
    }
}

function Wait-ArmOperation {
    param(
        [Parameter(Mandatory)]
        [string]$OperationUrl,

        [Parameter(Mandatory)]
        [hashtable]$Headers,

        [int]$MaximumAttempts = 90
    )

    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        $operation = Invoke-ArmRequest `
            -Method Get `
            -Url $OperationUrl `
            -Headers $Headers
        if (-not $operation.Success) {
            throw "APIM async operation query failed: $($operation.Body)"
        }
        $state = $operation.Body | ConvertFrom-Json
        if ($state.status -notin @('Accepted', 'InProgress', 'Running')) {
            return $state
        }
        Start-Sleep -Seconds 2
    }

    throw "APIM async operation did not complete after $MaximumAttempts attempts."
}

function Wait-ArmState {
    param(
        [Parameter(Mandatory)]
        [string]$Url,

        [Parameter(Mandatory)]
        [hashtable]$Headers,

        [Parameter(Mandatory)]
        [ValidateSet('Present', 'Absent')]
        [string]$State,

        [int]$MaximumAttempts = 60
    )

    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        $result = Invoke-ArmRequest -Method Get -Url $Url -Headers $Headers
        if ($State -eq 'Present' -and $result.Status -eq 200) {
            return
        }
        if ($State -eq 'Absent' -and $result.Status -eq 404) {
            return
        }
        Start-Sleep -Seconds 2
    }

    throw "Resource did not become $($State.ToLowerInvariant()): $Url"
}

Assert-DiagnosticName `
    -Name $apiName `
    -Pattern '^ai4ia-compiler-[0-9a-f]{12}$'
foreach ($definition in $fragmentDefinitions) {
    Assert-DiagnosticName `
        -Name $definition.TemporaryId `
        -Pattern '^ai4ia-compiler-(catalog-[0-9]|priority-[0-9]|setup)-[0-9a-f]{12}$'
    Assert-PolicyPayloadSize `
        -RelativePath $definition.Path `
        -MaximumBytes $fragmentPayloadMaxBytes `
        -LimitName 'APIM_POLICY_FRAGMENT_MAX_BYTES'
}
Assert-PolicyPayloadSize `
    -RelativePath $priorityPolicyPath `
    -MaximumBytes $policyDocumentMaxBytes `
    -LimitName 'APIM_POLICY_DOCUMENT_MAX_BYTES'
Assert-PolicyPayloadSize `
    -RelativePath $realtimePolicyPath `
    -MaximumBytes $policyDocumentMaxBytes `
    -LimitName 'APIM_POLICY_DOCUMENT_MAX_BYTES'

$accessToken = az account get-access-token `
    --subscription $SubscriptionId `
    --resource https://management.azure.com/ `
    --query accessToken `
    --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($accessToken)) {
    throw 'Unable to acquire an Azure Resource Manager access token.'
}
$headers = @{ Authorization = "Bearer $accessToken" }
$deleteHeaders = @{
    Authorization = "Bearer $accessToken"
    'If-Match' = '*'
}
$serviceUrl = "$managementBase`?api-version=$apiVersion"
$service = Invoke-ArmRequest -Method Get -Url $serviceUrl -Headers $headers
if (-not $service.Success) {
    throw "Target APIM service is unavailable: $($service.Body)"
}

$cleanupErrors = [System.Collections.Generic.List[string]]::new()
try {
    $api = Invoke-ArmRequest `
        -Method Put `
        -Url $apiUrl `
        -Headers $headers `
        -Payload @{
            properties = @{
                displayName = "AI4IA policy compiler diagnostic $suffix"
                path = $apiPath
                protocols = @('https')
                serviceUrl = 'https://example.com'
                subscriptionRequired = $false
                apiType = 'http'
            }
        }
    if (-not $api.Success) {
        throw "Temporary API creation failed: $($api.Body)"
    }
    Wait-ArmState -Url $apiUrl -Headers $headers -State Present
    Write-Information "Temporary API ready: $apiName" -InformationAction Continue

    foreach ($definition in $fragmentDefinitions) {
        $fragmentUrl = (
            "$managementBase/policyFragments/$($definition.TemporaryId)" +
            "?api-version=$apiVersion"
        )
        $fragmentPath = Join-Path $repoRoot $definition.Path
        $fragmentXml = Get-Content -LiteralPath $fragmentPath -Raw
        $fragment = Invoke-ArmRequest `
            -Method Put `
            -Url $fragmentUrl `
            -Headers $headers `
            -Payload @{
                properties = @{
                    description = (
                        "AI4IA compiler copy of $($definition.ProductionId) " +
                        "for run $suffix"
                    )
                    format = 'rawxml'
                    value = $fragmentXml
                }
            }
        if (-not $fragment.Success) {
            throw (
                "Temporary fragment creation failed for " +
                "$($definition.TemporaryId): $($fragment.Body)"
            )
        }
        $operationUrl = $fragment.Headers['Azure-AsyncOperation'] |
            Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($operationUrl)) {
            throw (
                "APIM did not return an async compiler operation for " +
                "$($definition.TemporaryId)."
            )
        }
        $state = Wait-ArmOperation `
            -OperationUrl $operationUrl `
            -Headers $headers
        if ($state.status -ne 'Succeeded') {
            throw (
                "APIM rejected fragment $($definition.TemporaryId): " +
                ($state.error | ConvertTo-Json -Depth 8 -Compress)
            )
        }
        Wait-ArmState -Url $fragmentUrl -Headers $headers -State Present
        Write-Information (
            "Temporary fragment compiled: $($definition.TemporaryId)"
        ) -InformationAction Continue
    }

    $policyXml = [System.IO.File]::ReadAllText(
        (Join-Path $repoRoot $priorityPolicyPath)
    )
    foreach ($definition in $fragmentDefinitions) {
        $productionReference = (
            'fragment-id="' + $definition.ProductionId + '"'
        )
        if (
            -not $policyXml.Contains($productionReference) -and
            $definition.ProductionId.StartsWith('endpoint_selection_')
        ) {
            throw (
                "$priorityPolicyPath does not include " +
                "$($definition.ProductionId)."
            )
        }
        if ($policyXml.Contains($productionReference)) {
            $policyXml = $policyXml.Replace(
                $productionReference,
                ('fragment-id="' + $definition.TemporaryId + '"')
            )
        }
    }
    $policy = Invoke-ArmRequest `
        -Method Put `
        -Url $apiPolicyUrl `
        -Headers $headers `
        -Payload @{
            properties = @{
                format = 'rawxml'
                value = $policyXml
            }
        }
    if (-not $policy.Success) {
        throw "Full temporary include chain failed compilation: $($policy.Body)"
    }

    Write-Information (
        'LIVE_APIM_FULL_CHAIN_COMPILER=PASS'
    ) -InformationAction Continue

    $realtimePolicyXml = [System.IO.File]::ReadAllText(
        (Join-Path $repoRoot $realtimePolicyPath)
    )
    $realtimePolicy = Invoke-ArmRequest `
        -Method Put `
        -Url $apiPolicyUrl `
        -Headers $headers `
        -Payload @{
            properties = @{
                format = 'rawxml'
                value = $realtimePolicyXml
            }
        }
    if (-not $realtimePolicy.Success) {
        throw "Realtime policy failed live compilation: $($realtimePolicy.Body)"
    }
    Write-Information (
        'LIVE_APIM_REALTIME_COMPILER=PASS'
    ) -InformationAction Continue
}
finally {
    $apiDelete = Invoke-ArmRequest `
        -Method Delete `
        -Url $apiUrl `
        -Headers $deleteHeaders
    if ($apiDelete.Status -notin @(200, 202, 204, 404)) {
        $cleanupErrors.Add(
            "API delete failed for $apiName (status $($apiDelete.Status))."
        )
    }
    try {
        Wait-ArmState -Url $apiUrl -Headers $headers -State Absent
        Write-Information (
            "CLEANUP_VERIFIED_ABSENT_API=$apiName"
        ) -InformationAction Continue
    }
    catch {
        $cleanupErrors.Add($_.Exception.Message)
    }

    foreach ($definition in $fragmentDefinitions) {
        $fragmentUrl = (
            "$managementBase/policyFragments/$($definition.TemporaryId)" +
            "?api-version=$apiVersion"
        )
        $fragmentDelete = Invoke-ArmRequest `
            -Method Delete `
            -Url $fragmentUrl `
            -Headers $deleteHeaders
        if ($fragmentDelete.Status -notin @(200, 202, 204, 404)) {
            $cleanupErrors.Add(
                "Fragment delete failed for $($definition.TemporaryId) " +
                "(status $($fragmentDelete.Status))."
            )
        }
        try {
            Wait-ArmState -Url $fragmentUrl -Headers $headers -State Absent
            Write-Information (
                "CLEANUP_VERIFIED_ABSENT_FRAGMENT=" +
                $definition.TemporaryId
            ) -InformationAction Continue
        }
        catch {
            $cleanupErrors.Add($_.Exception.Message)
        }
    }

    if ($cleanupErrors.Count -gt 0) {
        throw "Diagnostic cleanup failed: $($cleanupErrors -join ' ')"
    }
}
