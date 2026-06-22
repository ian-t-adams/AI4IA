@{
    # PSScriptAnalyzer settings for the AI4IA operational PowerShell scripts
    # (scripts/*.ps1). Consumed by .github/workflows/quality.yml.
    #
    # We fail the build on Warning and Error so real issues block a PR.
    Severity     = @('Error', 'Warning')

    ExcludeRules = @(
        # PSAvoidUsingWriteHost: these scripts are interactive operator CLIs
        # (inventory / teardown / seed-models / purge-soft-deleted / postprovision).
        # Colored, immediate Write-Host output is the deliberate UX. Write-Information
        # is suppressed by default (operators would see nothing) and Write-Output
        # would corrupt the pipeline values the helper functions return, so a rewrite
        # would be a behavior change. Excluded repo-wide by design.
        'PSAvoidUsingWriteHost'
    )
}
