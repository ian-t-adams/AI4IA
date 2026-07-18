# proxy — Model Gateway (SimpleL7Proxy + APIM)

The governed entry point for compatible HTTP/SSE model traffic. Applications call
SimpleL7Proxy; the proxy queues and forwards to APIM; APIM selects catalog-driven Foundry
backends with managed-identity auth. Voice Live/realtime WebSockets are the explicit exception:
the FastAPI relay calls the APIM realtime API directly because SimpleL7Proxy is not a WebSocket
proxy.

Upstream: https://github.com/microsoft/SimpleL7Proxy (.NET L7 proxy for Azure AI on Container
Apps; integrates App Configuration, App Insights, Event Hubs, Blob, Service Bus, APIM; Entra
App-ID gating + managed identity). **MIT licensed.**

## Vendored source

Vendored (not a submodule) from microsoft/SimpleL7Proxy @
`d9eb1d1fa42820792a9699bfc253562fba07d977` (2026-07-06):

- `Shared/` — shared library (PackageReferences only).
- `Shared-parser/` — config parser library (PackageReferences only).
- `SimpleL7Proxy/` — the proxy worker (.NET 10), references the two libraries above.
- `Dockerfile` — AI4IA-maintained multi-stage build with **build context = `./proxy`**. It keeps
  the upstream .NET 10/chiseled-runtime shape but also copies `Shared-parser.csproj` and
  `global.json` before restore, exposes AI4IA's `8080` listener plus the `9000` probe port, and
  starts the worker without the upstream web-host `--urls` argument. These are intentional local
  adaptations.

### Intentional source deviation

Four files carry AI4IA security/correctness patches over the audited pin:

- `SimpleL7Proxy/Config/IncomingAuthValidator.cs` applies `ValidateAuthConfig`'s `header=` value to the actual key lookup (upstream otherwise keeps
  reading the default `S7P-KEY`, which rejects AI4IA's `Ocp-Apim-Subscription-Key` ingress);
  it also fails startup for `oauth2`/`mixed` inbound mode until a trusted OIDC/JWKS signing-key source is
  implemented, rather than accepting unsigned JWTs.
- `SimpleL7Proxy/Config/ConfigFactory.cs` removes an upstream warm-reload debug line that printed
  old and new configuration values, which could expose a secret if an operator ever placed one in
  a warm App Configuration key.
- `SimpleL7Proxy/RequestData.cs` derives Azure-native deployment names from
  `/deployments/{name}/...` when the request body correctly omits `model`. This
  supplies the generated APIM catalog header for chat, embeddings, image, and
  audio calls while preserving body-based model detection for Responses API.
- `SimpleL7Proxy/server.cs`'s `ValidateAuthKey()` compares the incoming proxy auth key against
  `ValidateAuthKey1`/`ValidateAuthKey2` with a constant-time `SecretComparer.FixedTimeEquals`
  helper (new file: `SimpleL7Proxy/Config/SecretComparer.cs`) instead of upstream's
  `string.Equals(..., StringComparison.OrdinalIgnoreCase)`. These keys are opaque, high-entropy
  APIM subscription keys (see `gateway.bicep`'s `sharedProxyIngressSubscription.listSecrets().primaryKey`),
  not case-insensitive identifiers, and a non-constant-time comparison of a secret is a timing
  side-channel.

All other files in the three source directories remain byte-for-byte upstream. Re-evaluate and
drop the `IncomingAuthValidator.cs` patch when refreshing to an upstream commit that fixes both
behaviors it addresses.

**Provenance validation (2026-07-18, corrected):** an earlier version of this note claimed 170
byte-for-byte-identical files, based on SHA-256-hashing a `git checkout` of the pinned upstream
commit against the local working tree. That method is unreliable: git's checkout-time CRLF/LF
normalization (`core.autocrlf`) can make genuinely different stored content look identical (or
vice versa) once both sides pass through the same checkout filters. The corrected check compares
git blob SHA1s directly — via `git ls-tree`/`git cat-file` against the pinned upstream commit,
which read repository object-database bytes and never touch a working tree — across all 174 shared
files in `Shared/`, `Shared-parser/`, and `SimpleL7Proxy/`:

- **142 files** have an identical blob SHA1 (true byte-for-byte identity with upstream).
- **28 files** have a different blob SHA1, but identical content once CRLF/LF line endings are
  normalized on both sides: these are stored with CRLF locally and LF upstream. They are not
  functional deviations and are not part of the intentional-deviation list.
- **4 files** are the intentional deviations listed above; their diffs were re-confirmed against
  the pinned commit and match the descriptions given for each.
- **1 file** (`SimpleL7Proxy/Config/SecretComparer.cs`) is new, added locally, with no upstream
  counterpart.

142 + 28 + 4 = 174, matching the upstream file count exactly (174 + 1 new file = 175 local files).
Also confirmed via `git ls-remote https://github.com/microsoft/SimpleL7Proxy.git main` that upstream
`main` is still exactly `d9eb1d1fa42820792a9699bfc253562fba07d977` — the pin is the current
upstream tip, not a stale snapshot, so there is no newer commit to refresh to. Re-run the blob-level
comparison (not a working-tree hash) whenever the pin or the deviation list changes, and update
this note.

To refresh the vendored copy, check out the audited upstream commit and mirror the three project
directories from upstream `src/` (excluding `bin/`/`obj/`). Keep this README and the root
`Dockerfile`, reapply/test the documented source patches, verify every other source file is
byte-for-byte identical to upstream, and update both pin references.

## Runtime shape

- **Worker** (generic host, not a web host). The L7 `HttpListener` is bound to the
  `Port` env var (Bicep sets `8080`; Container Apps ingress `targetPort: 8080`) and
  serves `/readiness`, `/startup`, and `/liveness` on that listener.
- Token refresh runs as non-blocking background tasks, so the listener binds
  independently of backend token acquisition. Container Apps probes those
  endpoints on port `8080`.
- The backend comes from `Host1` (set in `infra/modules/gateway.bicep`) and targets APIM:
  `host=<apim-gateway>;mode=apim;probe=/openai/status;processor=OpenAI`. The APIM subscription key
  is a Container App secret exposed only through `Host1-api-key`; it is never embedded in `Host1`.
- APIM's system identity, not the proxy identity, holds Cognitive Services data-plane roles on
  Foundry. This makes APIM the only model-backend trust boundary for normal proxy traffic.

## Routing and retry ownership

The proxy forwards the **incoming request path verbatim** to APIM. A request to
`https://<proxy>/openai/deployments/<deployment>/...` therefore reaches the APIM `openai` API
with the full `/openai` path intact.

APIM uses the generated `infra/policies/simplel7proxy-endpoints.xml` catalog fragment to map each
catalog deployment name to every compatible regional deployment. It performs bounded immediate
regional failover and rewrites the deployment path/body to the selected region. If every eligible
backend is throttled, APIM returns the upstream SimpleL7Proxy contract (`429`,
`S7PREQUEUE: true`, `retry-after-ms`) and the proxy performs the delayed requeue.

`MaxAttempts=1` prevents retry multiplication: APIM owns immediate backend attempts inside one
proxy dispatch; the proxy owns delayed requeue, queue TTL, and its per-replica circuit breaker.
The synchronous queue is in-memory and per replica, so it is not a durable or globally ordered
fairness mechanism.

Three distinct APIM subscription keys separate these hops, each a Container App secret:

- The API authenticates to the proxy with a proxy-ingress key (`AI4IA_MODEL_GATEWAY_API_KEY`)
  scoped to an APIM product with no APIs attached, so it cannot invoke any model or realtime API
  even if leaked.
- The proxy authenticates to APIM's model API with its own key (`sharedProxyModelSubscription` in
  `gateway.bicep`), injected only into the proxy's `Host1` configuration and never exposed to the
  API; it strips the incoming ingress key before forwarding and injects this key instead.
- The FastAPI realtime relay authenticates directly to APIM's realtime WebSocket API with a third,
  separately scoped key (`AI4IA_REALTIME_GATEWAY_API_KEY`), bypassing the proxy entirely because it
  cannot proxy WebSockets.

`app/api`'s `Settings.validate_runtime()` fails startup if the realtime key and the proxy-ingress
key are ever set to the same value, so the two cannot be silently reused for each other. This
temporary key design is isolated per hop; the migration target is Entra workload authentication at
every edge.

## Optional controls

- App Configuration is read with `id-proxy`; warm profile, priority, and header
  policy values refresh without a revision. Event Hub and async settings are cold.
- Event Hub export is default-off and emits routing/status/latency metadata with
  request/response header logging disabled. It is not a work queue.
- Durable async is default-off and provisions dedicated MI-only Blob + Service Bus
  resources. It does not make the synchronous queue durable.
- Profiles are default-off and `UserConfigRequired=true` when enabled. The only
  supported source is the secret-mounted local snapshot. Validation blocks
  enablement until the edge derives a verified app identity; the proxy never
  reads Cosmos directly.

## Current scope

- SimpleL7Proxy + APIM front the Foundry endpoints with a `models.json`-derived
  allowlist, auth, routing, request IDs, multi-region selection, queueing/requeue,
  and App Insights. Priority reservations, Event Hub export, durable async, and
  profiles are optional and default off.
- Custom domain: `genaiproxy.nomad-analytics.com` (public, governed).
