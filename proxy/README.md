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
- `Dockerfile` — AI4IA-maintained multi-stage build with **build context = `./proxy`**. It uses
  digest-pinned .NET 10 SDK/chiseled-runtime bases, restores the checked-in NuGet locks with
  `--locked-mode`, exposes AI4IA's `8080` listener, and starts the worker without the upstream
  web-host `--urls` argument. These are intentional local adaptations. The upstream
  `SimpleL7Proxy/Dockerfile`, sample deployment, scratch file, and build helper remain
  provenance-tracked but are excluded from AI4IA's Docker build context and are never built.

### Intentional source deviation

Fourteen upstream files carry AI4IA security, correctness, dependency, or
telemetry patches over the audited pin. Four additional files are AI4IA-owned.
The complete machine-readable list and reason for every deviation lives in
`upstream-provenance.json`; the behaviorally important groups are:

- `SimpleL7Proxy/Config/IncomingAuthValidator.cs` applies `ValidateAuthConfig`'s `header=` value to the actual key lookup (upstream otherwise keeps
  reading the default `S7P-KEY`, which rejects AI4IA's `Ocp-Apim-Subscription-Key` ingress);
  it also fails startup for `oauth2`/`mixed` inbound mode until a trusted OIDC/JWKS signing-key source is
  implemented, rather than accepting unsigned JWTs.
- `SimpleL7Proxy/Config/ConfigFactory.cs` removes an upstream warm-reload debug line that printed
  old and new configuration values, which could expose a secret if an operator ever placed one in
  a warm App Configuration key. It additionally honours a new
  `ConfigOptionAttribute.Secret` flag when masking the startup "Configuration
  loaded" event, and its per-property masking loop is extracted into a public
  `BuildConfigSnapshot` so the redaction is directly testable. Upstream masks by
  substring-matching the key path (`connectionstring`/`password`/`secret`/
  `token`/`apikey`/`sas`); `Profiles:Auth:Key1` matches none of those, so the
  deployed proxy-ingress APIM subscription key was written to that event verbatim
  — and the default event client persists it to `eventslog.json`. Covered by
  `AI4IA.Proxy.Tests/ConfigRedactionTests.cs`.
- `SimpleL7Proxy/Config/ConfigMetadata.cs` adds the `Secret` flag described above.
  An explicit opt-in is used rather than widening the substring heuristic to
  `key`, because non-secret options legitimately contain that word
  (`Request:Headers:PriorityKeyHeader`, `Request:Priority:PriorityKeys`).
- `SimpleL7Proxy/Config/ProxyConfig.cs` marks `ValidateAuthKey1`/`ValidateAuthKey2`
  with that flag. These are the only two options that hold a credential.
- `SimpleL7Proxy/Config/AppConfigService.cs` checks a failed App Configuration
  download before dereferencing its nullable result. A transient failure now
  waits for the normal refresh interval instead of repeatedly throwing in a
  zero-delay loop.
- `Shared-parser/StreamProcessor/JsonStreamProcessor.cs` flushes the output
  `StreamWriter` after each line. Upstream's comment says "write each line
  immediately", but `WriteLineAsync` only fills the writer's 4 KiB char buffer,
  and the proxy's periodic `StreamFlusher` flushes the *underlying* stream, which
  cannot see characters still held in the writer. Because APIM sets
  `TOKENPROCESSOR` for `text/*` responses, every streaming chat completion went
  through this path and was withheld until ~4 KiB accumulated or the response
  ended. Covered by `AI4IA.Proxy.Tests/StreamProcessorFlushTests.cs`.
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
  side-channel. The listener also returns a fixed empty `404` for upstream's privileged legacy
  `/health`, `/healthdetail`, and `/forcegc` diagnostics before authentication, queueing, or
  worker dispatch. AI4IA exposes only the side-effect-free `/startup`, `/liveness`, and
  `/readiness` routes required by Container Apps; this prevents unauthenticated internal-state
  disclosure, counter resets, and forced blocking Gen-2 collections. The upstream request-null
  branch is also removed: `HttpListener.GetContextAsync()` and `HttpListenerContext.Request`
  are non-null contracts, while retaining that dead branch makes request data appear to control
  whether the later authentication methods execute (CodeQL `cs/user-controlled-bypass`).

The remaining declared deviations update the Application Insights 3.x /
OpenTelemetry integration, remove unused parser runtime packages, and keep the
runtime NuGet graph locked. All undeclared source files are upstream-identical
after line-ending normalization.
Re-evaluate and drop the `IncomingAuthValidator.cs` patch when refreshing to an
upstream commit that fixes both behaviors it addresses.

**Provenance validation (2026-08-10):** `upstream-provenance.json` records the
canonical LF SHA-256 of every upstream and local file plus the explicit AI4IA
patch list. Raw upstream hashes remain as evidence, but checkout-specific local
bytes never gate CI. `scripts/tests/test_proxy_provenance.py` fails for an
added, deleted, or semantically changed file that is not represented exactly.
The current measured breakdown is:

- **160 files** are content-equivalent to upstream after CRLF/LF canonicalization.
- **14 files** contain the documented AI4IA source patches.
- **4 files** are AI4IA additions: `Config/SecretComparer.cs` plus three
  `packages.lock.json` files used by the runtime project graph.

The upstream tree has 174 files; the local scoped tree has 178. Regenerate only
after fetching and reviewing the pinned upstream commit:

```powershell
git fetch --no-tags https://github.com/microsoft/SimpleL7Proxy.git d9eb1d1fa42820792a9699bfc253562fba07d977
python scripts/gen-proxy-provenance.py --upstream-ref FETCH_HEAD
python scripts/gen-proxy-provenance.py --check
```

**Pin currency (measured 2026-08-02): the pin is now STALE, and deliberately so.**
An earlier version of this note recorded that upstream `main` was still exactly
`d9eb1d1f…`, "so there is no newer commit to refresh to". That is no longer true, and the
note is corrected here rather than left to mislead. Per the GitHub compare API, upstream
`main` (`ea212ba563f54aa8de2aca35ae9f5c97baffe94a`, 2026-07-31) is **171 commits ahead and
0 behind** the pin. The same response lists 300 changed files, but 300 is the compare
endpoint's file cap — treat it as a floor, not a count. Latest upstream release at that
tip is **v2.2.17**.

Staying on the audited pin is a choice, not an oversight: the deployed gateway is healthy,
the local patches are written against this exact tree, and a refresh of this size is a
reviewed change with its own deploy — not a drive-by bump.

Verified drift in the files that matter (commits touching each path since the pin date;
every path below was confirmed to exist at the upstream tip first, because a path filter
that no longer matches returns a silent, misleading `0`):

| File | Upstream commits since pin | Refresh implication |
| --- | --- | --- |
| `Config/IncomingAuthValidator.cs` | **0** | The two behaviors this patch works around are **still unfixed upstream**, so the "re-evaluate and drop" note above is not yet actionable. Keep the patch. |
| `Config/ConfigFactory.cs` | 1 | Re-apply onto changed code. |
| `RequestData.cs` | 2 | Re-apply onto changed code; this is the `x-LLMModel` derivation AI4IA depends on. |
| `server.cs` | 1 | Re-apply onto changed code. |

Two upstream changes overlap behavior AI4IA has already had to fix, and should be read
before any refresh:

- **`rename priority to priorityGroup`**, plus `add priority tests`. AI4IA's Bicep sets
  `PriorityWorkers`, `DefaultPriority`, `PriorityKeys`, and `PriorityValues` (see
  `gateway.bicep`, and the `PriorityWorker`/`PriorityWorkers` singular-vs-plural trap
  documented there). All four names still appear upstream, so the rename looks additive
  rather than a removal — but priority semantics changed, and these keys are exactly where
  a silent no-op regression would land. Re-validate them against
  `PriorityWorkerConfigTests.cs` on refresh.
- **`fix probe dequeue bug, add tests`** and **`requeue bug fix`** — the proxy's requeue path
  is load-bearing for AI4IA's 429/`S7PREQUEUE` contract with APIM.

Also merged since the pin: the `feature/async` branch (several times, #205–#218) and a large
volume of documentation/UI work.

Regenerate the manifest whenever the pin or explicit patch list changes.

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
- ACA startup/readiness/liveness probes remain configured at 5/10/30-second
  intervals, but `EventType.Probe` is excluded from proxy console, App Insights,
  and event-log routing because upstream does not distinguish healthy hits from
  failures. ACA platform health/restart metrics remain enabled, and exception,
  circuit-breaker, and recovery warning signals are retained.
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

## Build and supply-chain verification

CI restores all four proxy projects from checked-in NuGet locks, builds/tests
them, and CodeQL analyzes the C# source. The PR image build resolves both
digest-pinned base images, scans the final loaded proxy image for HIGH/CRITICAL
findings using the CVE-specific `proxy/.trivyignore` policy, and retains an SPDX
SBOM plus build metadata for 30 days.

Repository secret/config scans also cover the vendored tree. Their only
upstream exceptions are exact-file/fingerprint entries whose paths are checked
against `upstream-provenance.json` as unpatched blobs.

The retained provenance is deliberately marked unsigned. Production signing and
verification would require an approved keyless identity policy or managed key
and a deploy-time verification design; those remain a roadmap gap rather than
new unreviewed Azure infrastructure in this repository.

## Current scope

- SimpleL7Proxy + APIM front the Foundry endpoints with a `models.json`-derived
  allowlist, auth, routing, request IDs, multi-region selection, queueing/requeue,
  and App Insights. Priority reservations, Event Hub export, durable async, and
  profiles are optional and default off.
- Custom domain: `genaiproxy.nomad-analytics.com` (public, governed).
