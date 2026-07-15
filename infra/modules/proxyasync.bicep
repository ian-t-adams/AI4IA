// Optional durable async backing for SimpleL7Proxy. The synchronous priority
// queue remains in-memory per replica; these resources are only for explicit
// async request/result workflows.
@description('Enable durable proxy async resources. Default OFF.')
param enabled bool = false

@description('Azure region for the async resources.')
param location string

@description('Tags applied to the async resources.')
param tags object

@description('Workload token used in resource names.')
param workload string

@description('Environment name used in resource names.')
param environmentName string

@description('Deterministic suffix for globally unique resource names.')
param uniqueSuffix string

@description('Proxy managed identity principal ID.')
param proxyPrincipalId string

@description('Service Bus queue used by SimpleL7Proxy async processing.')
param queueName string = 'requeststatus'

var storageName = take(toLower('st${replace(workload, '-', '')}async${uniqueSuffix}'), 24)
var serviceBusName = take('sb-${workload}-${environmentName}-proxy-async', 50)
var storageBlobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var serviceBusSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'

module asyncStorage 'br/public:avm/res/storage/storage-account:0.32.1' = if (enabled) {
  name: 'proxyAsyncStorage'
  params: {
    name: storageName
    location: location
    tags: tags
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    blobServices: {
      containers: [
        {
          name: 'requests'
          publicAccess: 'None'
        }
      ]
    }
    roleAssignments: [
      {
        principalId: proxyPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: storageBlobContributorRoleId
      }
    ]
  }
}

module asyncServiceBus 'br/public:avm/res/service-bus/namespace:0.16.2' = if (enabled) {
  name: 'proxyAsyncServiceBus'
  params: {
    name: serviceBusName
    location: location
    tags: tags
    skuObject: {
      name: 'Standard'
    }
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    minimumTlsVersion: '1.2'
    queues: [
      {
        name: queueName
      }
    ]
    roleAssignments: [
      {
        principalId: proxyPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: serviceBusSenderRoleId
      }
      {
        principalId: proxyPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: serviceBusReceiverRoleId
      }
    ]
  }
}

#disable-next-line BCP318
output blobServiceUri string = enabled ? asyncStorage.outputs.primaryBlobEndpoint : ''
#disable-next-line BCP318
output serviceBusNamespaceFqdn string = enabled ? '${asyncServiceBus.outputs.name}.servicebus.windows.net' : ''
output asyncQueueName string = enabled ? queueName : ''
