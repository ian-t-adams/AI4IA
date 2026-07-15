// VNet + private DNS for the optional network-isolation hardening pass.
// Provisioned only when main.bicep's `vnetIsolationEnabled` flag is on; nothing
// here exists by default. Hosts the Container Apps env (snet-infra) and the
// private endpoints for the data tier (snet-pep), plus the four private DNS
// zones their FQDNs resolve through.
@description('Location for the network resources.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('VNet address space. /22 leaves room for the env (/23) + PE subnet (/27) + growth.')
param addressSpace string = '10.40.0.0/22'

@description('Infrastructure subnet for the Container Apps environment. Consumption-only envs require a dedicated, undelegated /23.')
param infraSubnetPrefix string = '10.40.0.0/23'

@description('Subnet that holds the data-tier private endpoints.')
param pepSubnetPrefix string = '10.40.2.0/27'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${workload}-${environmentName}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressSpace
      ]
    }
    subnets: [
      {
        // Container Apps (Consumption-only) infrastructure subnet: dedicated,
        // undelegated, /23. The env binds to this at creation time.
        name: 'snet-infra'
        properties: {
          addressPrefix: infraSubnetPrefix
        }
      }
      {
        // Private-endpoint subnet. Network policies disabled is required so PE
        // NICs can be placed here.
        name: 'snet-pep'
        properties: {
          addressPrefix: pepSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

// Private DNS zones the data-tier private endpoints register A records in. Linked
// to the VNet (no auto-registration) so the apps resolve the private IPs.
var zoneNames = [
  'privatelink.documents.azure.com' // Cosmos DB (Sql)
  'privatelink.blob.${environment().suffixes.storage}' // Storage (blob)
  'privatelink.vaultcore.azure.net' // Key Vault
  'privatelink.servicebus.windows.net' // Service Bus
]

resource dnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for z in zoneNames: {
  name: z
  location: 'global'
  tags: tags
}]

resource dnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (z, i) in zoneNames: {
  parent: dnsZones[i]
  name: 'link-${workload}-${environmentName}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}]

output vnetId string = vnet.id
output infraSubnetId string = '${vnet.id}/subnets/snet-infra'
output pepSubnetId string = '${vnet.id}/subnets/snet-pep'
output cosmosDnsZoneId string = dnsZones[0].id
output blobDnsZoneId string = dnsZones[1].id
output vaultDnsZoneId string = dnsZones[2].id
output serviceBusDnsZoneId string = dnsZones[3].id
