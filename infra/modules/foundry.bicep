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

// Annotate-only Responsible AI policy. Every category, on both the prompt and the
// completion, is left ENABLED (so the model still emits content-safety
// annotations) but with blocking turned OFF — input and output are never blocked,
// only labeled. The four harm categories use the maximum severity threshold with
// blocking:false; the optional Microsoft.DefaultV2 filters that ship blocking-ON
// (Jailbreak / Prompt Shield, Protected Material Text/Code) are explicitly
// overridden to blocking:false or they would still block. Every model deployment
// in this account references this policy by name (see models.bicep) so the
// annotate-only posture is uniform across all models.
resource annotateOnlyRaiPolicy 'Microsoft.CognitiveServices/accounts/raiPolicies@2024-10-01' = {
  parent: account
  name: 'ai4ia-annotate-only'
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    contentFilters: [
      { name: 'hate', blocking: false, enabled: true, severityThreshold: 'High', source: 'Prompt' }
      { name: 'sexual', blocking: false, enabled: true, severityThreshold: 'High', source: 'Prompt' }
      { name: 'selfharm', blocking: false, enabled: true, severityThreshold: 'High', source: 'Prompt' }
      { name: 'violence', blocking: false, enabled: true, severityThreshold: 'High', source: 'Prompt' }
      { name: 'hate', blocking: false, enabled: true, severityThreshold: 'High', source: 'Completion' }
      { name: 'sexual', blocking: false, enabled: true, severityThreshold: 'High', source: 'Completion' }
      { name: 'selfharm', blocking: false, enabled: true, severityThreshold: 'High', source: 'Completion' }
      { name: 'violence', blocking: false, enabled: true, severityThreshold: 'High', source: 'Completion' }
      { name: 'jailbreak', blocking: false, enabled: true, source: 'Prompt' }
      { name: 'protected_material_text', blocking: false, enabled: true, source: 'Completion' }
      { name: 'protected_material_code', blocking: false, enabled: true, source: 'Completion' }
    ]
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
output raiPolicyName string = annotateOnlyRaiPolicy.name
