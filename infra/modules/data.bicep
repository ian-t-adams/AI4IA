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

@description('Central Log Analytics workspace resource id. Diagnostic settings stream data-store logs/metrics there for the admin observability plane.')
param logAnalyticsWorkspaceId string

@description('Tenant ID for Entra auth on Postgres.')
param tenantId string = subscription().tenantId

@description('Deploy the Postgres Flexible Server (pgvector home for mem0). Disable where the subscription is offer-restricted for Postgres.')
param deployPostgres bool = true

@description('Location for the Postgres Flexible Server (may differ from `location` due to subscription offer restrictions).')
param postgresLocation string = location

@description('Provision the document library blob storage account + container. Gated on the document-understanding flag so nothing is created by default — zero regression.')
param deployDocumentStorage bool = false

@description('Blob container holding the raw + parsed + chunk artifacts of the document library.')
param documentBlobContainer string = 'documents'

@description('Provision a dedicated, short-lived container on the document storage account for inline-attachment ORIGINAL bytes (inline code interpreter, default OFF). Separate from the durable library container; carries a blob lifecycle TTL so retained originals auto-expire.')
param deployInlineAttachmentStorage bool = false

@description('Blob container holding inline-attachment original bytes, scoped per-user+session as {userId}/{sessionId}/{documentId}. Short-lived (lifecycle TTL); never the durable corpus.')
param inlineAttachmentBlobContainer string = 'ephemeral-attachments'

@description('Provision the generated-image blob storage account + container. Gated on the image-generation flag so nothing is created by default. Independent of the document library storage — zero regression to either.')
param deployImageStorage bool = false

@description('Blob container holding tool-generated images, scoped per-user as {userId}/generated/{id}.png.')
param imageBlobContainer string = 'images'

@description('Provision the generated-video blob container. Gated on the video-generation flag. Reuses the generated-media storage account (shared with images) and adds a dedicated videos container — the account-scoped RBAC already covers it.')
param deployVideoStorage bool = false

@description('Blob container holding tool-generated videos, scoped per-user as {userId}/generated/{id}.mp4.')
param videoBlobContainer string = 'videos'

@description('''Public network access for the data tier (Cosmos + both storage
accounts). 'Enabled' (default) keeps today's public + identity-gated posture.
'Disabled' makes the data tier private-only: only valid once
the private endpoints exist AND the deployer has a VNet path, or azd loses the
ability to manage these resources. Driven by main.bicep's `dataTierPrivate` flag.''')
@allowed([
  'Enabled'
  'Disabled'
])
param dataPublicNetworkAccess string = 'Enabled'

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
    // Required: the api Container App runs on the Consumption plan with public egress
    // and no VNet integration, so it reaches Cosmos over public networking (data-plane
    // auth is still AAD-only via managed identity + the Built-in Data Contributor role
    // assigned below). NOTE: a tenant policy remediation (`CosmosDB_LocalAuth_Modify`,
    // which enforces disableLocalAuth) can issue a control-plane PATCH that drifts this
    // to 'Disabled', which severs the api from Cosmos (every Cosmos-backed endpoint 500s
    // when VNet isolation is enabled. Re-running `azd provision` re-asserts
    // 'Enabled'. To be drift-proof / policy-compliant instead, move to a VNet-integrated
    // Container Apps environment + a Cosmos private endpoint (a later hardening pass).
    publicNetworkAccess: dataPublicNetworkAccess
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
  {
    name: 'mcpServers'
    partitionKey: '/userId'
  }
  {
    name: 'usage'
    partitionKey: '/userId'
  }
  {
    name: 'entitlements'
    partitionKey: '/userId'
  }
  {
    // Admin-only directory mapping the hashed internal userId -> display name +
    // email, captured from token claims going forward (no historical backfill).
    name: 'userDirectory'
    partitionKey: '/userId'
  }
  {
    name: 'documents'
    partitionKey: '/sessionId'
  }
  {
    name: 'userDocuments'
    partitionKey: '/userId'
  }
  {
    name: 'analyzers'
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
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05' // Monitoring Reader

resource cosmosDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, apiPrincipalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: apiPrincipalId
    scope: cosmos.id
  }
}

// Monitoring Reader so the api can read Azure Monitor metrics (RU consumption,
// latency, availability) for the admin dashboard Cosmos resource panel.
resource apiCosmosMonitoringRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(cosmos.id, apiPrincipalId, monitoringReaderRoleId)
  scope: cosmos
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Stream Cosmos control-plane operations (config/RBAC/firewall changes — the
// security audit trail) and all platform metrics (RU consumption, latency,
// availability) to the central Log Analytics workspace. The very-high-volume
// `DataPlaneRequests` category is deliberately excluded to keep ingestion
// cost-aware; per-request signal is already carried by the api's own structured
// usage telemetry. Retention follows the workspace (30 days).
resource cosmosDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: cosmos
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'ControlPlaneRequests', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---------------- Postgres Flexible Server (pgvector) ----------------
// Name includes the location so a region change yields a fresh resourceId (ARM
// enforces location-immutability per resourceId; a prior eastus2 attempt otherwise
// blocks re-creation in another region).
var postgresName = take('psql-${workload}-${environmentName}-${postgresLocation}-${uniqueSuffix}', 60)

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (deployPostgres) {
  name: postgresName
  location: postgresLocation
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
resource postgresAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = if (deployPostgres) {
  parent: postgres
  name: apiPrincipalId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: apiPrincipalName
    tenantId: tenantId
  }
}

// Allowlist the pgvector extension (app runs CREATE EXTENSION vector at init).
resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = if (deployPostgres) {
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

resource memoryDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (deployPostgres) {
  parent: postgres
  name: 'mem0'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  // Serialize server child operations. Flexible Server runs control-plane ops one
  // at a time; when a database create is in flight the server is briefly "not
  // accessible", which makes a concurrent Entra-admin op fail with
  // AadAuthOperationCannotBePerformedWhenServerIsNotAccessible. Chaining after the
  // admin (via postgresExtensions) keeps the whole sequence single-file.
  dependsOn: [
    postgresExtensions
  ]
}

// Allow Azure-internal traffic (the special 0.0.0.0 rule) so the api Container
// App — Consumption plan, public egress, no VNet integration — can reach the
// server. DB auth is still AAD-only; this only opens the network firewall to
// Azure-origin sources. Tighten to a VNet/private endpoint in a later hardening pass.
resource postgresAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (deployPostgres) {
  parent: postgres
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  // Last link in the serialized child-operation chain (see memoryDb) so the
  // firewall update never runs concurrently with the Entra-admin assignment.
  dependsOn: [
    memoryDb
  ]
}

// Stream the Postgres server log (errors/connections/checkpoints — the standard
// operational signal, and the cheapest-to-export log category) plus all platform
// metrics (CPU/memory/storage/connections) to the central Log Analytics
// workspace. Verbose query-store/session categories are deliberately excluded for
// cost. Only created with the server. Retention follows the workspace (30 days).
resource postgresDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployPostgres) {
  name: 'to-log-analytics'
  scope: postgres
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'PostgreSQLLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// Monitoring Reader so the api can read Azure Monitor metrics (CPU/memory/storage/
// connections) for the admin dashboard Postgres resource panel.
resource apiPostgresMonitoringRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployPostgres) {
  name: guid(postgres.id, apiPrincipalId, monitoringReaderRoleId)
  scope: postgres
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------- Document library blob storage ----------------
// Provisioned only when document understanding is enabled (deployDocumentStorage).
// AAD-only (no account keys), private container, TLS 1.2+. Raw uploads and the
// parsed/chunk artifacts live under {userId}/{documentId}/... — the userId prefix
// is the per-user isolation boundary mirroring the Cosmos partition key.
// uniqueString() returns a stable 13-char alphanumeric hash; 'st' + it is a valid
// (15-char, lowercase) globally-unique storage account name, no take() needed.
var documentStorageName = 'st${uniqueString(resourceGroup().id)}'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource documentStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (deployDocumentStorage) {
  name: documentStorageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: dataPublicNetworkAccess
    networkAcls: {
      defaultAction: dataPublicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource documentBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = if (deployDocumentStorage) {
  parent: documentStorage
  name: 'default'
  properties: {}
}

resource documentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployDocumentStorage) {
  parent: documentBlobService
  name: documentBlobContainer
  properties: {
    publicAccess: 'None'
  }
}

// Dedicated EPHEMERAL container for inline-attachment original bytes (inline code
// interpreter; default OFF). Kept clearly apart from the durable
// library container so the short-lived originals never mingle with the corpus, and
// a blob lifecycle rule (below) is the durable TTL backstop in addition to the
// app's own delete-on-document-delete / purge-on-session-delete cleanup.
resource inlineAttachmentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployDocumentStorage && deployInlineAttachmentStorage) {
  parent: documentBlobService
  name: inlineAttachmentBlobContainer
  properties: {
    publicAccess: 'None'
  }
}

// Lifecycle TTL: hard-expire anything left in the ephemeral container after 1 day.
// Belt-and-suspenders behind the app's explicit cleanup — guarantees no inline
// original lingers even if a delete is missed. Scoped by the container-name prefix
// so it never touches the durable library container on the same account.
resource documentBlobLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = if (deployDocumentStorage && deployInlineAttachmentStorage) {
  parent: documentStorage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-ephemeral-attachments'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                inlineAttachmentBlobContainer
              ]
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterCreationGreaterThan: 1
                }
              }
            }
          }
        }
      ]
    }
  }
}

// Blob data-plane RBAC: the api identity gets Storage Blob Data Contributor on the
// account (read/write/delete the user library artifacts via AAD; no account keys).
resource documentStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployDocumentStorage) {
  name: guid(documentStorage.id, apiPrincipalId, storageBlobDataContributorRoleId)
  scope: documentStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Stream blob-service mutation logs (write/delete — the audit-worthy operations)
// + all blob metrics (transactions, latency, availability, capacity) to the
// central Log Analytics workspace. Storage data-plane logs live on the blob
// service sub-resource, not the account. The very-high-volume `StorageRead`
// category is deliberately excluded for cost. Only created with the account.
resource documentStorageDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployDocumentStorage) {
  name: 'to-log-analytics'
  scope: documentBlobService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---------------- Generated-media blob storage ----------------
// A single dedicated media account backs both the generate_image and
// generate_video tools, fully independent of the document library so
// enabling either tool never touches the library's storage. Provisioned when
// EITHER tool is enabled; each tool's container is created only when its own flag
// is on. Same hardening: AAD-only (no account keys), private containers, TLS 1.2+.
// Artifacts live under {userId}/generated/{id}.{ext} — the userId prefix is the
// per-user isolation boundary enforced by the authenticated serve endpoints.
var imageStorageName = 'sti${uniqueString(resourceGroup().id)}'
var deployMediaStorage = deployImageStorage || deployVideoStorage

resource imageStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (deployMediaStorage) {
  name: imageStorageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: dataPublicNetworkAccess
    networkAcls: {
      defaultAction: dataPublicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource imageBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = if (deployMediaStorage) {
  parent: imageStorage
  name: 'default'
  properties: {}
}

resource imageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployImageStorage) {
  parent: imageBlobService
  name: imageBlobContainer
  properties: {
    publicAccess: 'None'
  }
}

resource videoContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployVideoStorage) {
  parent: imageBlobService
  name: videoBlobContainer
  properties: {
    publicAccess: 'None'
  }
}

resource imageStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployMediaStorage) {
  name: guid(imageStorage.id, apiPrincipalId, storageBlobDataContributorRoleId)
  scope: imageStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Same cost-aware blob diagnostics as the document account, for the shared
// generated-media account (images + videos). Created when either media tool is on.
resource imageStorageDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployMediaStorage) {
  name: 'to-log-analytics'
  scope: imageBlobService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output cosmosAccountName string = cosmos.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDb.name
output postgresName string = deployPostgres ? postgres.name : ''
output postgresFqdn string = postgres.?properties.fullyQualifiedDomainName ?? ''
output postgresDatabaseName string = deployPostgres ? memoryDb.name : ''
output documentBlobAccountUrl string = documentStorage.?properties.primaryEndpoints.blob ?? ''
output documentBlobContainerName string = documentBlobContainer
output inlineAttachmentBlobContainerName string = inlineAttachmentBlobContainer
output imageBlobAccountUrl string = imageStorage.?properties.primaryEndpoints.blob ?? ''
output imageBlobContainerName string = imageBlobContainer
output videoBlobAccountUrl string = imageStorage.?properties.primaryEndpoints.blob ?? ''
output videoBlobContainerName string = videoBlobContainer

// Resource IDs consumed by the private-endpoint module (network-isolation pass).
// Conditional storage accounts return '' when not deployed; main.bicep filters
// empty IDs before building the PE target array.
output cosmosId string = cosmos.id
output postgresId string = deployPostgres ? postgres.id : ''
output documentStorageId string = deployDocumentStorage ? documentStorage.id : ''
output imageStorageId string = deployMediaStorage ? imageStorage.id : ''
