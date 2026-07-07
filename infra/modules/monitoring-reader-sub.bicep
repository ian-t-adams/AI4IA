// Grants a managed identity Monitoring Reader at SUBSCRIPTION scope.
//
// The admin dashboard's resource panels read Azure Monitor platform metrics via the
// batch metrics API (metrics:getBatch), which requires Monitoring Reader at
// subscription scope — per-resource grants are NOT sufficient. Monitoring Reader is
// read-only and this subscription is dedicated to AI4IA, so a single subscription
// grant is acceptable and replaces the four former per-resource grants.
//
// This lives in its own subscription-targetScoped module because a subscription
// role-assignment name (a guid over the principalId) must be calculable at the start
// of the deployment. That holds when the principalId arrives as a module parameter,
// but not when it is dereferenced from a module output inline in main.bicep (BCP120).
targetScope = 'subscription'

@description('Principal ID (objectId) of the managed identity to grant Monitoring Reader.')
param principalId string

var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05' // Monitoring Reader

resource apiMonitoringReaderSub 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, principalId, monitoringReaderRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
  }
}
