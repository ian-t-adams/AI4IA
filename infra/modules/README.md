# Bicep modules

Composable modules consumed by [`../main.bicep`](../main.bicep). Added incrementally in
Phase 1+ — see [`../README.md`](../README.md) for the module map. Each module:

- targets `resourceGroup` scope (the RG created by `main.bicep`),
- accepts a `tags` object and applies it to every resource,
- exposes typed `output`s consumed by downstream modules,
- avoids inline secrets (managed identity + Key Vault/App Config only).
