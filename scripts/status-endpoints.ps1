function Resolve-ApiStatusTarget {
    param([string] $Url, [object[]] $Resources)

    if (-not $Url) {
        $apps = @($Resources | Where-Object {
            $_.type -ieq 'microsoft.app/containerapps' -and $_.service -ceq 'api'
        })
        if ($apps.Count -ne 1) {
            $reason = if ($apps.Count -eq 0) { 'target_unresolved' } else { 'target_ambiguous' }
            return @{
                url = ''; outcome = $reason
                note = 'API URL unresolved: expected one Container App tagged azd-service-name=api.'
            }
        }
        if ($apps[0].ingressExternal -ine 'true') {
            return @{
                url = ''; outcome = 'private_ingress'
                note = 'API ingress is not explicitly public; no public health probe was attempted.'
            }
        }
        $hostName = [string]$apps[0].ingressFqdn
        if ([uri]::CheckHostName($hostName) -ne [UriHostNameType]::Dns) {
            return @{
                url = ''; outcome = 'invalid_target'
                note = 'API inventory did not supply a valid ingress DNS name.'
            }
        }
        $Url = "https://$hostName"
    }

    $uri = $null
    if (-not [uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -cne 'https' -or -not $uri.IsDefaultPort -or
        $uri.HostNameType -ne [UriHostNameType]::Dns -or
        -not $uri.Host.Contains('.') -or $uri.Host.EndsWith('.localhost') -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
        return @{
            url = ''; outcome = 'invalid_target'
            note = 'API URL must be an HTTPS DNS origin without credentials, a path, query or fragment.'
        }
    }
    return @{ url = $uri.AbsoluteUri.TrimEnd('/'); outcome = ''; note = '' }
}

function Invoke-ApiHealthRequest {
    param(
        [Parameter(Mandatory)][string] $Url,
        [System.Net.Http.HttpClient] $Client
    )

    $ownsClient = $null -eq $Client
    $request = $null
    $response = $null
    $stream = $null
    $deadline = [System.Threading.CancellationTokenSource]::new([TimeSpan]::FromSeconds(20))
    try {
        if ($ownsClient) {
            $handler = [System.Net.Http.HttpClientHandler]::new()
            $handler.AllowAutoRedirect = $false
            $handler.UseCookies = $false
            $handler.UseDefaultCredentials = $false
            $Client = [System.Net.Http.HttpClient]::new($handler)
        }
        $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Url)
        $request.Headers.Accept.ParseAdd('application/json')
        $request.Headers.CacheControl = [System.Net.Http.Headers.CacheControlHeaderValue]::new()
        $request.Headers.CacheControl.NoCache = $true
        $response = $Client.SendAsync(
            $request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead, $deadline.Token
        ).GetAwaiter().GetResult()
        $result = @{
            httpStatus = [int]$response.StatusCode
            contentType = [string]$response.Content.Headers.ContentType.MediaType
            body = ''; bodyValid = $false
        }
        if ($result.contentType -ine 'application/json' -or $response.Content.Headers.ContentLength -gt 4096) {
            return $result
        }

        # Read one extra byte to distinguish a complete bounded body from truncation.
        $buffer = [byte[]]::new(4097)
        $length = 0
        $stream = $response.Content.ReadAsStreamAsync($deadline.Token).GetAwaiter().GetResult()
        while ($length -lt $buffer.Length) {
            $read = $stream.ReadAsync(
                $buffer, $length, $buffer.Length - $length, $deadline.Token
            ).GetAwaiter().GetResult()
            if ($read -eq 0) { break }
            $length += $read
        }
        if ($length -le 4096) {
            try {
                $result.body = [System.Text.UTF8Encoding]::new($false, $true).GetString($buffer, 0, $length)
                $result.bodyValid = $true
            } catch [System.Text.DecoderFallbackException] {
                $result.bodyValid = $false
            }
        }
        return $result
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($response) { $response.Dispose() }
        if ($request) { $request.Dispose() }
        if ($ownsClient -and $Client) { $Client.Dispose() }
        $deadline.Dispose()
    }
}

function Test-ApiHealthEndpoint {
    param(
        [Parameter(Mandatory)][ValidateSet('liveness', 'readiness')][string] $Kind,
        [Parameter(Mandatory)][System.Collections.IDictionary] $Target
    )

    $path = if ($Kind -eq 'liveness') { '/health/live' } else { '/health/ready' }
    $result = [ordered]@{
        name = "API $Kind"; kind = $Kind
        url = ''; httpStatus = 0; ok = $false; state = 'unknown'
        outcome = $Target.outcome; note = $Target.note
        observedAt = $null; latencyMs = $null
    }
    if (-not $Target.url) { return [pscustomobject]$result }
    $result.url = "$($Target.url)$path"
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-ApiHealthRequest -Url $result.url
        $result.httpStatus = $response.httpStatus
        if ($response.httpStatus -in 401,403) {
            $result.outcome = 'auth_required'
            $result.note = 'Authentication blocked the public health check; health is not established.'
        } elseif ($response.httpStatus -ge 300 -and $response.httpStatus -lt 400) {
            $result.outcome = 'unexpected_redirect'
            $result.note = 'Health endpoint redirected; redirects are not followed.'
        } elseif ($response.httpStatus -ne 200 -and
            -not ($Kind -eq 'readiness' -and $response.httpStatus -eq 503)) {
            $result.state = 'down'
            $result.outcome = 'http_error'
            $result.note = 'Health endpoint returned an unsuccessful HTTP status; failure stage is unconfirmed.'
        } else {
            $validShape = $false
            $validStage = $Kind -eq 'liveness'
            $statusValue = ''
            if ($response.bodyValid -and $response.contentType -ieq 'application/json') {
                $document = $null
                try {
                    $options = [System.Text.Json.JsonDocumentOptions]::new()
                    $options.MaxDepth = 4
                    $document = [System.Text.Json.JsonDocument]::Parse([string]$response.body, $options)
                    $root = $document.RootElement
                    if ($root.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
                        $statusProperty = [System.Text.Json.JsonElement]::new()
                        $stageProperty = [System.Text.Json.JsonElement]::new()
                        $validShape = $root.TryGetProperty('status', [ref]$statusProperty) -and
                            $statusProperty.ValueKind -eq [System.Text.Json.JsonValueKind]::String
                        if ($validShape) { $statusValue = $statusProperty.GetString() }
                        if ($Kind -eq 'readiness') {
                            $validStage = $root.TryGetProperty('stage', [ref]$stageProperty) -and
                                $stageProperty.ValueKind -eq [System.Text.Json.JsonValueKind]::String -and
                                $stageProperty.GetString() -ceq 'session_store'
                        }
                        $names = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                        foreach ($property in $root.EnumerateObject()) {
                            if (-not $names.Add($property.Name)) { $validShape = $false }
                        }
                    }
                } catch [System.Text.Json.JsonException] {
                    $validShape = $false
                } finally {
                    if ($document) { $document.Dispose() }
                }
            }
            if ($validShape -and $validStage -and $response.httpStatus -eq 200 -and $statusValue -ceq 'ok') {
                $result.ok = $true
                $result.state = 'up'
                $result.outcome = 'healthy'
                $result.note = if ($Kind -eq 'liveness') {
                    'API process responded; authentication, persistence and model traffic are not covered.'
                } else {
                    'Cached canonical session-store readiness passed; authentication, Search and model traffic are not covered.'
                }
            } elseif ($validShape -and $validStage -and $Kind -eq 'readiness' -and
                $response.httpStatus -eq 503 -and $statusValue -ceq 'unavailable') {
                $result.state = 'down'
                $result.outcome = 'persistence_unavailable'
                $result.note = 'API reports its canonical session store unavailable.'
            } else {
                $result.outcome = 'invalid_response'
                $result.note = 'Expected bounded API health JSON was not received; health is not established.'
            }
        }
    } catch [System.Net.Http.HttpRequestException], [System.OperationCanceledException], [System.IO.IOException] {
        $result.outcome = 'network_unavailable'
        $result.note = 'No complete health response (network failure or 20-second deadline); health is not established.'
    } finally {
        $timer.Stop()
        $result.latencyMs = $timer.ElapsedMilliseconds
        $result.observedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    return [pscustomobject]$result
}
