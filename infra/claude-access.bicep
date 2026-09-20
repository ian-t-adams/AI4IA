targetScope = 'resourceGroup'

@description('Separate TARGET-tenant operator approval. No access is created by default.')
param grantInferenceAccess bool = false

@minLength(3)
param accountName string

@minLength(36)
@maxLength(36)
@description('Observed target-tenant SERVICE PRINCIPAL object ID for the source multitenant application. Never its client ID or the source UAMI principal ID.')
param targetPrincipalId string

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

// Documented Foundry inference role. Provider metadata alone does not prove
// live Claude authorization; the approved governed canary remains a rollout gate.
resource inferenceRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = if (grantInferenceAccess) {
  name: guid(resourceGroup().id, account.id, 'ai4ia-claude-maas')
  properties: {
    roleName: 'AI4IA Claude inference ${accountName}'
    description: 'MaaS inference on the isolated Claude account; no keys, secrets or management actions.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: ['Microsoft.CognitiveServices/accounts/MaaS/*']
        notDataActions: []
      }
    ]
  }
}

resource inferenceAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantInferenceAccess) {
  name: guid(account.id, targetPrincipalId, 'ai4ia-claude-maas')
  scope: account
  properties: {
    roleDefinitionId: inferenceRole!.id
    principalId: targetPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output roleDefinitionId string = grantInferenceAccess ? inferenceRole!.id : ''
