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
# Additive coverage for the Speech Voice Live onHandshake policy
# (infra/policies/speech-voice-live.xml). This is a second, independent
# temporary WebSocket API -- distinct name/path from the HTTP diagnostic API
# above -- because onHandshake policies can only be attached to a WebSocket
# API's auto-created onHandshake operation, not an HTTP API's API-scoped
# policy. It does not touch the production speech-voice-live-realtime API,
# its subscription, or any named value.
$speechWsApiName = "ai4ia-compiler-ws-$suffix"
$speechWsApiPath = $speechWsApiName
$speechWsApiUrl = "$managementBase/apis/$speechWsApiName`?api-version=$apiVersion"
$speechWsHandshakePolicyUrl = (
    "$managementBase/apis/$speechWsApiName/operations/onHandshake/policies/policy" +
    "?api-version=$apiVersion"
)
$speechVoiceLivePolicyPath = 'infra/policies/speech-voice-live.xml'
$fragmentDefinitions = @(
    @{
        ProductionId = 'endpoint_selection_catalog_0_32'
        TemporaryId = "ai4ia-compiler-catalog-0-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-0.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_1_32'
        TemporaryId = "ai4ia-compiler-catalog-1-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-1.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_2_32'
        TemporaryId = "ai4ia-compiler-catalog-2-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-2.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_3_32'
        TemporaryId = "ai4ia-compiler-catalog-3-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-3.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_4_32'
        TemporaryId = "ai4ia-compiler-catalog-4-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-4.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_5_32'
        TemporaryId = "ai4ia-compiler-catalog-5-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-5.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_6_32'
        TemporaryId = "ai4ia-compiler-catalog-6-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-6.xml'
    }
    @{
        ProductionId = 'endpoint_selection_catalog_7_32'
        TemporaryId = "ai4ia-compiler-catalog-7-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints-catalog-7.xml'
    }
    @{
        ProductionId = 'endpoint_selection_setup_32'
        TemporaryId = "ai4ia-compiler-setup-$suffix"
        Path = 'infra/policies/simplel7proxy-endpoints.xml'
    }
    @{
        ProductionId = 'simplel7proxy_inbound_pre_32'
        TemporaryId = "ai4ia-compiler-inbound-pre-$suffix"
        Path = 'infra/policies/simplel7proxy_inbound_pre_32.xml'
    }
    @{
        ProductionId = 'simplel7proxy_inbound_post_32'
        TemporaryId = "ai4ia-compiler-inbound-post-$suffix"
        Path = 'infra/policies/simplel7proxy_inbound_post_32.xml'
    }
    @{
        ProductionId = 'simplel7proxy_backend_32'
        TemporaryId = "ai4ia-compiler-backend-$suffix"
        Path = 'infra/policies/simplel7proxy_backend_32.xml'
    }
    @{
        ProductionId = 'simplel7proxy_outbound_32'
        TemporaryId = "ai4ia-compiler-outbound-$suffix"
        Path = 'infra/policies/simplel7proxy_outbound_32.xml'
    }
    @{
        ProductionId = 'simplel7proxy_on_error_32'
        TemporaryId = "ai4ia-compiler-on-error-$suffix"
        Path = 'infra/policies/simplel7proxy_on_error_32.xml'
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
Assert-DiagnosticName `
    -Name $speechWsApiName `
    -Pattern '^ai4ia-compiler-ws-[0-9a-f]{12}$'
foreach ($definition in $fragmentDefinitions) {
    Assert-DiagnosticName `
        -Name $definition.TemporaryId `
        -Pattern (
            '^ai4ia-compiler-(catalog-[0-3]|setup|inbound-pre|' +
            'inbound-post|backend|outbound|on-error)-[0-9a-f]{12}$'
        )
}

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

    $policyPath = Join-Path `
        $repoRoot `
        'infra/policies/simplel7proxy-priority-policy.xml'
    $policyXml = Get-Content -LiteralPath $policyPath -Raw
    foreach ($definition in $fragmentDefinitions) {
        $policyXml = $policyXml.Replace(
            $definition.ProductionId,
            $definition.TemporaryId
        )
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
    $policyOperationUrl = $policy.Headers['Azure-AsyncOperation'] |
        Select-Object -First 1
    if (-not [string]::IsNullOrWhiteSpace($policyOperationUrl)) {
        $policyState = Wait-ArmOperation `
            -OperationUrl $policyOperationUrl `
            -Headers $headers
        if ($policyState.status -ne 'Succeeded') {
            throw (
                'Full temporary include chain failed compilation: ' +
                ($policyState.error | ConvertTo-Json -Depth 8 -Compress)
            )
        }
    }

    Write-Information 'LIVE_APIM_COMPILER=PASS' -InformationAction Continue

    # Speech Voice Live onHandshake policy coverage (additive; independent of
    # the fragment-based include chain above). onHandshake is APIM's immutable,
    # auto-created operation on a WebSocket API, so a distinct temporary
    # WebSocket API is required to exercise it -- it cannot be attached to the
    # HTTP diagnostic API created above.
    $speechWsApi = Invoke-ArmRequest `
        -Method Put `
        -Url $speechWsApiUrl `
        -Headers $headers `
        -Payload @{
            properties = @{
                displayName = "AI4IA Speech Voice Live policy compiler diagnostic $suffix"
                path = $speechWsApiPath
                protocols = @('wss')
                serviceUrl = 'wss://example.com'
                subscriptionRequired = $false
                type = 'websocket'
            }
        }
    if (-not $speechWsApi.Success) {
        throw "Temporary WebSocket API creation failed: $($speechWsApi.Body)"
    }
    Wait-ArmState -Url $speechWsApiUrl -Headers $headers -State Present
    Write-Information "Temporary WebSocket API ready: $speechWsApiName" -InformationAction Continue

    $speechVoiceLivePolicyFullPath = Join-Path $repoRoot $speechVoiceLivePolicyPath
    $speechVoiceLivePolicyXml = Get-Content -LiteralPath $speechVoiceLivePolicyFullPath -Raw
    $speechWsPolicy = Invoke-ArmRequest `
        -Method Put `
        -Url $speechWsHandshakePolicyUrl `
        -Headers $headers `
        -Payload @{
            properties = @{
                format = 'rawxml'
                value = $speechVoiceLivePolicyXml
            }
        }
    if (-not $speechWsPolicy.Success) {
        throw "Speech Voice Live onHandshake policy failed compilation: $($speechWsPolicy.Body)"
    }
    $speechWsPolicyOperationUrl = $speechWsPolicy.Headers['Azure-AsyncOperation'] |
        Select-Object -First 1
    if (-not [string]::IsNullOrWhiteSpace($speechWsPolicyOperationUrl)) {
        $speechWsPolicyState = Wait-ArmOperation `
            -OperationUrl $speechWsPolicyOperationUrl `
            -Headers $headers
        if ($speechWsPolicyState.status -ne 'Succeeded') {
            throw (
                'Speech Voice Live onHandshake policy failed compilation: ' +
                ($speechWsPolicyState.error | ConvertTo-Json -Depth 8 -Compress)
            )
        }
    }

    Write-Information 'LIVE_APIM_SPEECH_VOICE_LIVE_COMPILER=PASS' -InformationAction Continue
}
finally {
    $speechWsApiDelete = Invoke-ArmRequest `
        -Method Delete `
        -Url $speechWsApiUrl `
        -Headers $deleteHeaders
    if ($speechWsApiDelete.Status -notin @(200, 202, 204, 404)) {
        $cleanupErrors.Add(
            "WebSocket API delete failed for $speechWsApiName (status $($speechWsApiDelete.Status))."
        )
    }
    try {
        Wait-ArmState -Url $speechWsApiUrl -Headers $headers -State Absent
        Write-Information (
            "CLEANUP_VERIFIED_ABSENT_API=$speechWsApiName"
        ) -InformationAction Continue
    }
    catch {
        $cleanupErrors.Add($_.Exception.Message)
    }

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
