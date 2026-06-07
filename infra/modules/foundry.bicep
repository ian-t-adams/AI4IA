// Foundry (Azure AI Services) account + default project for one region.
@description('Azure region for this Foundry account.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Foundry (Cognitive Services AIServices) account name.')
param accountName string

@description('Default project name.')
param projectName string

@description('Disable local (key) auth in favor of Entra/managed identity.')
param disableLocalAuth bool = false

@description('Principal IDs granted data-plane access (Cognitive Services OpenAI User + User).')
param dataPlanePrincipalIds array = []

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: toLower(accountName)
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: disableLocalAuth
    allowProjectManagement: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'AI4IA default project (${location}).'
  }
}

// Data-plane RBAC for app identities (managed-identity model access; no keys).
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
var cognitiveUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User

resource openAiUserAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in dataPlanePrincipalIds: {
  name: guid(account.id, pid, openAiUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

resource cognitiveUserAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in dataPlanePrincipalIds: {
  name: guid(account.id, pid, cognitiveUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveUserRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

output accountId string = account.id
output accountName string = account.name
output endpoint string = account.properties.endpoint
output projectName string = project.name
output principalId string = account.identity.principalId
