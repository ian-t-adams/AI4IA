// Azure Durable Task Scheduler (DTS): durable execution for multi-step workflows.
//
// Why this and not Azure Functions / Durable Functions: DTS is deliberately
// DECOUPLED from compute. The orchestrator and activities run inside the API
// container app we already deploy, on the same image, identity, and networking.
// Adopting Durable Functions would mean a second compute platform, a second
// deployment pipeline, and a second identity/RBAC surface for one feature.
//
// Provisioned ONLY when enableDurableWorkflows is true. The param DEFAULT is
// false everywhere, because this is a PAID resource: a fresh consumer of this
// template provisions no scheduler and is billed nothing. Turning it on is a
// deliberate operator action.
//
// Model calls made from a durable run still go proxy -> APIM -> Foundry. DTS
// carries orchestration state only; it is not in the model path.
@description('Location for the Durable Task Scheduler.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Per-subscription uniqueness suffix. The scheduler endpoint is public DNS (https://<name>.<region>.durabletask.io), so the name is globally unique across Azure and a redeploy into a *different* subscription would collide with the environment that already holds the unsuffixed name.')
param uniqueSuffix string

@description('Name of the task hub inside the scheduler. One hub per environment; runs in different hubs are fully isolated from each other.')
param taskHubName string = 'ai4ia'

@description('Principal IDs granted data-plane access to the task hub (the API container app identity). Each gets Durable Task Data Contributor, scoped to the hub rather than the scheduler so a second app on the same scheduler cannot read this hub.')
param dataContributorPrincipalIds array = []

@description('SKU. Consumption is pay-per-use and is the right default for a feature that is off unless an operator turns it on; Dedicated buys reserved capacity and predictable latency for sustained load.')
@allowed([
  'Consumption'
  'Dedicated'
])
param skuName string = 'Consumption'

@description('SKU capacity (Dedicated only; ignored for Consumption).')
param skuCapacity int = 1

@description('IP allow list for the scheduler data plane. REQUIRED by the resource provider. Defaults to open because Container Apps egress IPs are dynamic without VNet integration, so any narrower literal list would silently lock the API out on the next scale event. This is defence in depth, NOT the primary control: the data plane is Entra-authenticated and every caller still needs an explicit Durable Task Data Contributor grant below. Narrow it once the API runs behind a NAT gateway or VNet with a stable egress IP.')
param ipAllowlist array = [
  '0.0.0.0/0'
]

// Durable Task Data Contributor. Read/write orchestration instances: schedule,
// query, terminate, purge. This is the role the API needs for both the client
// (schedule/get_status) and the worker (dequeue/complete work items).
// https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/integration#durable-task-data-contributor
var durableTaskDataContributorRoleId = '0ad04412-c4d5-4796-b79c-f76d14c8d402'

resource scheduler 'Microsoft.DurableTask/schedulers@2026-02-01' = {
  // Pattern is ^[a-zA-Z0-9-]{3,64}$ -- alphanumeric and hyphen only, so no
  // underscores or dots may be introduced into the tokens that build this.
  name: take('dts-${workload}-${environmentName}-${uniqueSuffix}', 64)
  location: location
  tags: tags
  properties: {
    ipAllowlist: ipAllowlist
    // capacity is only meaningful for Dedicated (fixed monthly cost per Capacity
    // Unit, and it drives zone redundancy). Consumption bills per action
    // dispatched, so capacity has nothing to scale. Omitted there rather than
    // sent-and-hoped: this resource is provisioned only behind an operator flag,
    // so a first deploy that the RP rejects would surface as a failed change
    // window, not as something CI could have caught.
    sku: skuName == 'Dedicated'
      ? {
          name: skuName
          capacity: skuCapacity
        }
      : {
          name: skuName
        }
  }
}

resource taskHub 'Microsoft.DurableTask/schedulers/taskHubs@2026-02-01' = {
  parent: scheduler
  name: taskHubName
}

// Scoped to the TASK HUB, not the scheduler: a future second app sharing this
// scheduler gets its own hub and cannot read this one's orchestration payloads
// (which carry user prompts and model output).
resource dataContributorAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in dataContributorPrincipalIds: {
  name: guid(taskHub.id, pid, durableTaskDataContributorRoleId)
  scope: taskHub
  properties: {
    principalId: pid
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', durableTaskDataContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}]

@description('Scheduler resource name.')
output schedulerName string = scheduler.name

@description('Scheduler data-plane endpoint. READ from the resource rather than rebuilt from the name + region: the hostname format is a service detail, and a hand-built string would keep deploying happily while pointing nowhere if it ever changed.')
output endpoint string = scheduler.properties.endpoint

@description('Task hub name, passed to the app as AI4IA_DURABLE_TASK_HUB_NAME.')
output taskHubName string = taskHub.name
