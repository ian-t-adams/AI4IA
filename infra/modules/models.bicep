// Model deployments for one Foundry account, driven by infra/models.json.
// Deployments are created sequentially (@batchSize(1)) because Cognitive Services
// rejects concurrent deployment operations on the same account.
@description('Existing Foundry account name to deploy models into.')
param accountName string

@description('''Flat list of deployments for this account. Each item:
{ deploymentName: string, modelName: string, format: string, version: string, sku: string, capacity: int }''')
param deployments array

@description('Name of the Responsible AI policy applied to every deployment (annotate-only).')
param raiPolicyName string = ''

@description('Legal entity name used for Anthropic Marketplace attestation.')
param claudeOrganizationName string

@description('ISO-2 country code used for Anthropic Marketplace attestation.')
param claudeCountryCode string

@description('Lowercase industry value used for Anthropic Marketplace attestation.')
param claudeIndustry string

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = [for d in deployments: {
  parent: account
  name: d.deploymentName
  sku: {
    name: d.sku
    capacity: d.capacity
  }
  properties: union({
    model: {
      format: d.format
      name: d.modelName
      version: d.version
    }
    // models.json pins an explicit version and is the reviewed source of truth.
    // Auto-upgrade would change model behavior without a repository diff,
    // evaluation, or deploy and could not be undone by container rollback.
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: empty(raiPolicyName) ? null : raiPolicyName
  }, d.format == 'Anthropic' ? {
    // Required by the Cognitive Services RP for Claude. Supplying this block
    // accepts Anthropic Marketplace terms, so values are explicit deploy
    // parameters and the preprovision validator refuses blanks/placeholders.
    modelProviderData: {
      organizationName: claudeOrganizationName
      countryCode: claudeCountryCode
      industry: claudeIndustry
    }
  } : {})
}]

output deploymentNames array = [for (d, i) in deployments: d.deploymentName]
