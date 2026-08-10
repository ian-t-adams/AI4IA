$script:AzureCliSubscription = $null

function Invoke-AzureCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $effectiveArguments = @($Arguments)
    if (-not [string]::IsNullOrWhiteSpace($script:AzureCliSubscription) -and
        $effectiveArguments[0] -ne 'account') {
        $subscriptionIndex = [Array]::IndexOf($effectiveArguments, '--subscription')
        if ($subscriptionIndex -ge 0) {
            if ($subscriptionIndex + 1 -ge $effectiveArguments.Count -or
                [string]$effectiveArguments[$subscriptionIndex + 1] -cne $script:AzureCliSubscription) {
                throw "Azure CLI invocation attempted to override the verified subscription."
            }
        } else {
            $effectiveArguments += @('--subscription', $script:AzureCliSubscription)
        }
    }

    $output = @(& az @effectiveArguments)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Azure CLI failed with exit code ${exitCode}: az $($effectiveArguments -join ' ')"
    }
    return $output
}

function Assert-AzureSubscription {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $Subscription
    )

    Invoke-AzureCli -Arguments @('account', 'set', '--subscription', $Subscription) | Out-Null
    $activeSubscription = @(
        Invoke-AzureCli -Arguments @('account', 'show', '--query', 'id', '--output', 'tsv')
    )

    if ($activeSubscription.Count -ne 1 -or
        [string]$activeSubscription[0] -cne $Subscription) {
        $actual = if ($activeSubscription.Count -eq 1) {
            [string]$activeSubscription[0]
        } else {
            "<$($activeSubscription.Count) values>"
        }
        throw "Active Azure subscription '$actual' does not exactly match requested subscription '$Subscription'."
    }
    $script:AzureCliSubscription = $Subscription
}
