# Bicep modules

Composable modules consumed by [`../main.bicep`](../main.bicep). Each module:

- targets `resourceGroup` scope unless it must operate at subscription scope,
- accepts and applies shared `tags`,
- exposes typed outputs consumed by downstream modules,
- uses managed identity, Key Vault, or Container App secrets instead of inline
  secrets,
- wires diagnostic settings to the shared Log Analytics workspace where supported.

Gateway-specific modules:

- `gateway.bicep` owns the public SimpleL7Proxy Container App, the internal APIM
  model and realtime APIs, scoped subscriptions, the bounded API-policy wrapper,
  ordered generated fragments, and APIM-to-Foundry RBAC.
- `proxyasync.bicep` owns optional MI-only Blob/Service Bus resources for durable
  async work. It is not the synchronous priority queue, which remains in-memory
  per proxy replica.
