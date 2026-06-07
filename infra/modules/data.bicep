// Application data stores: Cosmos DB (NoSQL, canonical app data) + Postgres
// Flexible Server (pgvector home for mem0). Identity-based auth only (no keys/passwords).
@description('Location for the data stores.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Deterministic alphanumeric suffix for globally-unique names.')
param uniqueSuffix string

@description('Principal ID (objectId) of the api identity granted data-plane access.')
param apiPrincipalId string

@description('Resource name of the api identity (used as the Postgres AAD admin login name).')
param apiPrincipalName string

@description('Tenant ID for Entra auth on Postgres.')
param tenantId string = subscription().tenantId

// ---------------- Cosmos DB (NoSQL) ----------------
var cosmosAccountName = take('cosmos-${workload}-${environmentName}-${uniqueSuffix}', 44)

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosAccountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: 'Enabled'
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'ai4ia'
  properties: {
    resource: {
      id: 'ai4ia'
    }
  }
}

// Canonical app data: partition by user where possible; messages by session.
var containers = [
  {
    name: 'users'
    partitionKey: '/id'
  }
  {
    name: 'sessions'
    partitionKey: '/userId'
  }
  {
    name: 'messages'
    partitionKey: '/sessionId'
  }
  {
    name: 'agents'
    partitionKey: '/userId'
  }
  {
    name: 'workflows'
    partitionKey: '/userId'
  }
]

resource cosmosContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for c in containers: {
  parent: cosmosDb
  name: c.name
  properties: {
    resource: {
      id: c.name
      partitionKey: {
        paths: [
          c.partitionKey
        ]
        kind: 'Hash'
        version: 2
      }
    }
  }
}]

// Cosmos data-plane RBAC: api identity gets the built-in Data Contributor role.
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource cosmosDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, apiPrincipalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: apiPrincipalId
    scope: cosmos.id
  }
}

// ---------------- Postgres Flexible Server (pgvector) ----------------
var postgresName = take('psql-${workload}-${environmentName}-${uniqueSuffix}', 60)

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B2s'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    highAvailability: {
      mode: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
  }
}

// Entra admin = api managed identity (no SQL passwords).
resource postgresAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: apiPrincipalId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: apiPrincipalName
    tenantId: tenantId
  }
}

// Allowlist the pgvector extension (app runs CREATE EXTENSION vector at init).
resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
  dependsOn: [
    postgresAdmin
  ]
}

resource memoryDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'mem0'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

output cosmosAccountName string = cosmos.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDb.name
output postgresName string = postgres.name
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output postgresDatabaseName string = memoryDb.name
