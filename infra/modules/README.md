# Bicep modules

Composable modules consumed by [`../main.bicep`](../main.bicep). Each module:

- targets `resourceGroup` scope unless it must operate at subscription scope,
- accepts and applies shared `tags`,
- exposes typed outputs consumed by downstream modules,
- uses managed identity, Key Vault, or Container App secrets instead of inline
  secrets,
- wires diagnostic settings to the shared Log Analytics workspace where supported.

Gateway-specific modules:

- `apimcore.bicep` owns the unconditional shared `apim-mcp-*` Basic v2 service,
  its system identity, and diagnostic setting.
- `mcpgateway.bicep` references that service as existing and owns the feature-gated
  official MCP backends/APIs/policies/product/key.
- `gateway.bicep` references that service as existing and owns the public
  SimpleL7Proxy app plus model/realtime API children, scoped subscriptions, generated
  fragments, and Foundry role assignments. The Consumption APIM remains rollback-only.
- `proxyasync.bicep` owns optional MI-only Blob/Service Bus resources for durable
  async work. It is not the synchronous priority queue, which remains in-memory
  per proxy replica.
