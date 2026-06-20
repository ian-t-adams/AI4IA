// Telemetry/eventing backbone: Event Hubs namespace + hub for cost/usage events.
// Identity-based auth only (local/SAS auth disabled).
@description('Location for the namespace.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Deterministic alphanumeric suffix for globally-unique names.')
@minLength(8)
param uniqueSuffix string

@description('Principal IDs granted Event Hubs Data Sender (api/proxy emit telemetry).')
param senderPrincipalIds array = []

@description('Principal IDs granted Event Hubs Data Receiver (consumers of telemetry).')
param receiverPrincipalIds array = []

@description('Central Log Analytics workspace resource id. Diagnostic settings stream namespace logs/metrics there for the admin observability plane.')
param logAnalyticsWorkspaceId string

var namespaceName = take('evhns-${workload}-${environmentName}-${uniqueSuffix}', 50)

resource namespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 1
  }
  properties: {
    minimumTlsVersion: '1.2'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource telemetryHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: namespace
  name: 'telemetry'
  properties: {
    partitionCount: 4
    messageRetentionInDays: 1
  }
}

var dataSenderRoleId = '2b629674-e913-4c01-ae53-ef4638d8f975' // Azure Event Hubs Data Sender
var dataReceiverRoleId = 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde' // Azure Event Hubs Data Receiver

resource senderAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in senderPrincipalIds: {
  name: guid(namespace.id, pid, dataSenderRoleId)
  scope: namespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dataSenderRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

resource receiverAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in receiverPrincipalIds: {
  name: guid(namespace.id, pid, dataReceiverRoleId)
  scope: namespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dataReceiverRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

// Stream namespace operational logs + all platform metrics to the central Log
// Analytics workspace. The `telemetry` hub is dormant today, so volume is near
// zero; `OperationalLogs` is the cost-aware management-plane category (verbose
// archive/Kafka categories are deliberately excluded). Retention follows the
// workspace (30 days).
resource namespaceDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: namespace
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'OperationalLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output namespaceName string = namespace.name
output namespaceFqdn string = '${namespace.name}.servicebus.windows.net'
output telemetryHubName string = telemetryHub.name
