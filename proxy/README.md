# proxy — Model Gateway (SimpleL7Proxy + APIM)

The governed entry point for **all** model traffic. The backend calls APIM; APIM forwards to
SimpleL7Proxy; the proxy routes to Foundry across regions (managed-identity auth, `mode=direct`)
and emits cost/token telemetry.

Upstream: https://github.com/microsoft/SimpleL7Proxy (.NET L7 proxy for Azure AI on Container
Apps; integrates App Configuration, App Insights, Event Hubs, Blob, Service Bus, APIM; Entra
App-ID gating + managed identity). **MIT licensed.**

## Vendored source

Vendored (not a submodule) from microsoft/SimpleL7Proxy @ `72244ac3e779587ae81e25de3ece4653cfbf2ab1`:

- `Shared/` — shared library (PackageReferences only).
- `Shared-parser/` — config parser library (PackageReferences only).
- `SimpleL7Proxy/` — the proxy worker (.NET 10), references the two libraries above.
- `Dockerfile` — multi-stage build, **build context = `./proxy`**. Differs from the upstream
  `SimpleL7Proxy/Dockerfile` (whose context is the repo `src/`) only in that it also copies
  `Shared-parser/Shared-parser.csproj` before `dotnet restore` — the proxy references it, so
  the upstream file fails restore without it.

To refresh the vendored copy, re-clone upstream and re-copy the three project dirs (exclude
`bin/`/`obj/`); keep this README and the `Dockerfile`.

## Runtime shape

- **Worker** (generic host, not a web host). The L7 listener is an `HttpListener` bound to the
  `Port` env var (Bicep sets `8080`; Container Apps ingress `targetPort: 8080`). A separate
  Kestrel **probe server** listens on `9000` (`/health`, `/readiness`, `/startup`, `/liveness`).
- Token refresh runs as non-blocking background tasks, so the listener binds and the Container
  Apps TCP probe on 8080 passes even before the first backend token is acquired.
- Backends come from `Host1..HostN` env vars (set in `infra/modules/gateway.bicep`):
  `host=<foundry-endpoint>;usemi=true;audience=https://cognitiveservices.azure.com;mode=direct`.
  The proxy's managed identity holds Cognitive Services OpenAI User + Cognitive Services User on
  every Foundry account (granted in `infra/modules/foundry.bicep` via `dataPlanePrincipalIds`).

## Routing / cutover

The proxy forwards the **incoming request path verbatim** to the chosen Foundry host
(`BuildDestinationUrl` sets the backend path = the path it received) and overwrites the
`Authorization` header with its own MI bearer token plus the correct `Host` header.

Therefore APIM must hand the proxy the full Azure OpenAI path. APIM (`infra/modules/gateway.bicep`,
`modelsApi.serviceUrl`):

- **Pre-cutover (direct):** `serviceUrl = '<foundry>/openai'` — APIM calls Foundry directly with
  its own MI token.
- **Cutover (through proxy):** `serviceUrl = '<proxyUrl>/openai'` — APIM forwards
  `/openai/deployments/{model}/...` to the proxy, which rebuilds `<foundry>/openai/deployments/...`.
  The bare `<proxyUrl>` (no `/openai`) is wrong: the proxy would forward `/deployments/...` with
  no `/openai` prefix and Foundry returns 404.

Verify the proxy in isolation **before** cutover by POSTing directly to its external ingress:
`https://<ca-proxy-fqdn>/openai/deployments/<model>/chat/completions?api-version=...` (no auth —
the proxy injects MI auth). A real completion confirms proxy → Foundry works end-to-end. Only
then flip `serviceUrl`; re-run a live chat turn through the api; revert if it breaks.

## Plan

- **Phase 1.5 (minimal gateway):** SimpleL7Proxy + minimal APIM in front of the Foundry
  endpoints with a `models.json`-derived allowlist, auth, routing, request IDs, token/cost
  telemetry. Backend targets this from the start so model calls aren't wired twice.
- **Phase 6 (advanced governance):** multi-region capacity sharing, priority queues, richer cost
  analytics to Event Hubs/App Insights, and user entitlements (App Configuration + `auth.json`).
- Custom domain: `genaiproxy.nomad-analytics.com` (public, governed) — not fully private for v1.
