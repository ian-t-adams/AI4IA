# proxy — Model Gateway (SimpleL7Proxy + APIM)

The governed entry point for **all** model traffic. The backend calls the gateway; the gateway
routes to Foundry across regions and emits cost/token telemetry.

Upstream: https://github.com/microsoft/SimpleL7Proxy (.NET L7 proxy for Azure AI on Container
Apps; integrates App Configuration, App Insights, Event Hubs, Blob, Service Bus, APIM; Entra
App-ID gating + managed identity).

## Plan
- **Phase 1.5 (minimal gateway):** stand up SimpleL7Proxy + a minimal APIM in front of the
  Foundry endpoints with a `models.json`-derived allowlist, auth, routing, request IDs, and
  token/cost telemetry. Backend targets this from the start so model calls aren't wired twice.
- **Phase 6 (advanced governance):** multi-region capacity sharing, priority queues, richer
  cost analytics to Event Hubs/App Insights, and user entitlements.

## Integration approach
- Vendor or submodule SimpleL7Proxy here and add AI4IA-specific config (routes, backends,
  headers). Container build (`Dockerfile`) → Azure Container Apps, fronted by APIM.
- Custom domain: `genaiproxy.nomad-analytics.com` (public, governed) — not fully private for v1.

> This directory currently documents intent; the proxy is vendored/configured in Phase 1.5.
