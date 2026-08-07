// Foundry (Azure AI Services) account + default project for one region.
@description('Azure region for this Foundry account.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Foundry (Cognitive Services AIServices) account name.')
param accountName string

@description('Default project name.')
param projectName string

@description('''Disable local (key) auth on the Foundry accounts in favour of
Entra/managed identity. Default TRUE: gateway-only routing is only a real
boundary when an account key cannot bypass it.

Verified before flipping (2026-08-06) that nothing reaches a Foundry account with
a key. APIM authenticates with its managed identity (37 `auth: MI` entries in the
generated catalog, zero `api-key`); Content Understanding and the Responses-API
Code Interpreter both default to `bearer` (`cu_auth_mode`,
`code_interpreter_auth_mode`) and neither `AI4IA_CU_API_KEY` nor
`AI4IA_CODE_INTERPRETER_API_KEY` is set in production; and every key-bearing env
var on `ca-api-*` is a SimpleL7Proxy ingress key, an APIM subscription key or a
third-party key -- none is a Cognitive Services account key.

Set false only if a live deploy proves an unforeseen key dependency. Doing so
re-opens the bypass, so record why.''')
param disableLocalAuth bool = true

@description('Principal IDs granted data-plane access (Cognitive Services OpenAI User + User).')
param dataPlanePrincipalIds array = []

@description('Principal IDs granted the "Foundry User" role on the PROJECT (Agent Service data plane: toolbox/agent invocation). Default empty; only populated for the primary account when the Foundry-toolbox bridge is enabled (no role assignment while empty).')
param toolboxPrincipalIds array = []

@description('Central Log Analytics workspace resource id. Diagnostic settings stream account logs/metrics there for the admin observability plane.')
param logAnalyticsWorkspaceId string

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
// Content Understanding, scoped to the ``MultiModalIntelligence`` data plane and
// nothing else. This deliberately REPLACES `Cognitive Services User`
// (a97b65f3-…), whose single dataAction is the wildcard
// `Microsoft.CognitiveServices/*` — a strict superset of every OpenAI inference
// action, including `deployments/chat/completions/action` and `responses/*`.
//
// That wildcard is why the P1-4 remediation had to be resequenced. Content
// Understanding is enabled in production and needs a
// `cognitiveservices.azure.com` token, so `Cognitive Services User` could not
// simply be dropped — and while it stays, removing `Cognitive Services OpenAI
// User` from an identity accomplishes **nothing**, because the wildcard still
// grants direct inference. Narrowing this role first is what makes the OpenAI
// grant the only inference path, and therefore what makes removing it mean
// something.
var contentUnderstandingRoleId = '59a2dba3-6303-4fd8-9a2e-8cbb4bdda972' // Cognitive Services Content Understanding Contributor
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Foundry User (formerly "Azure AI User") — Agent Service data plane (toolbox/agent invocation)

resource openAiUserAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in dataPlanePrincipalIds: {
  name: guid(account.id, pid, openAiUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

resource contentUnderstandingAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in dataPlanePrincipalIds: {
  name: guid(account.id, pid, contentUnderstandingRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contentUnderstandingRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

// Project-scoped "Foundry User" grants for identities that invoke the Agent
// Service data plane (the toolbox MCP endpoint at
// {project_endpoint}/toolboxes/<name>/mcp). Unlike the account-scoped model
// grants above, the toolbox authorizes on the PROJECT resource. Preview
// capability; default-empty (no grant is created unless the bridge is enabled).
resource toolboxFoundryUserAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in toolboxPrincipalIds: {
  name: guid(project.id, pid, foundryUserRoleId)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

// Stream the account's audit + request/response logs and all platform metrics to
// the central Log Analytics workspace (the model data plane: who called which
// model, content-safety annotations, latency/throttling). Deliberately omits the
// high-volume `Trace` category to keep ingestion cost-aware; retention follows the
// workspace (30 days), so no per-setting retention policy is configured.
resource accountDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: account
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'Audit', enabled: true }
      { category: 'RequestResponse', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output accountId string = account.id
output accountName string = account.name
output endpoint string = account.properties.endpoint
output projectName string = project.name
// Foundry Agent Service project (data-plane) endpoint. The Agent Service host is
// `<subdomain>.services.ai.azure.com` (distinct from the account's
// `.cognitiveservices.azure.com` inference endpoint), and the subdomain is
// deterministically `toLower(accountName)` (customSubDomainName above), so this is
// composed rather than read from an ARM property. Consumed by the Foundry-toolbox
// bridge: the toolbox MCP URL is `<projectEndpoint>/toolboxes/<name>/mcp`.
output projectEndpoint string = 'https://${toLower(accountName)}.services.ai.azure.com/api/projects/${project.name}'
output principalId string = account.identity.principalId
output raiPolicyName string = annotateOnlyRaiPolicy.name
