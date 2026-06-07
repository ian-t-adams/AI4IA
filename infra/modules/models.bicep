// Model deployments for one Foundry account, driven by infra/models.json.
// Deployments are created sequentially (@batchSize(1)) because Cognitive Services
// rejects concurrent deployment operations on the same account.
@description('Existing Foundry account name to deploy models into.')
param accountName string

@description('''Flat list of deployments for this account. Each item:
{ deploymentName: string, modelName: string, format: string, sku: string, capacity: int }''')
param deployments array

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [for d in deployments: {
  parent: account
  name: d.deploymentName
  sku: {
    name: d.sku
    capacity: d.capacity
  }
  properties: {
    model: {
      format: d.format
      name: d.modelName
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}]

output deploymentNames array = [for (d, i) in deployments: d.deploymentName]
