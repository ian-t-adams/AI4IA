# Bicep modules

Composable modules consumed by [`../main.bicep`](../main.bicep). Each module:

- targets `resourceGroup` scope unless it must operate at subscription scope,
- accepts and applies shared `tags`,
- exposes typed outputs consumed by downstream modules,
- uses managed identity, Key Vault, or Container App secrets instead of inline
  secrets,
- wires diagnostic settings to the shared Log Analytics workspace where supported.
