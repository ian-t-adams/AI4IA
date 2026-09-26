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
`b0066b0e53f89abb5e84cfeacda2fdcaca8b081e` (2026-09-18, upstream v2.3.0):

- `Shared/` — shared library (PackageReferences only).
- `Shared-parser/` — config parser library (PackageReferences only).
- `SimpleL7Proxy/` — the proxy worker (.NET 10), references the two libraries above.
- `CompanionApp/` — an optional, hosted subset of upstream's Blazor telemetry console: the
  Event Hub monitor and Insights pages only. Every upstream page and asset AI4IA does not
  vendor is recorded as `ai4ia-excluded` with its upstream hash and reason. See
  [CompanionApp telemetry console](#companionapp-telemetry-console-optional).
- `Dockerfile` — AI4IA-maintained multi-stage build with **build context = `./proxy`**. It uses
  digest-pinned .NET 10 SDK/chiseled-runtime bases, restores the checked-in NuGet locks with
  `--locked-mode`, exposes AI4IA's `8080` listener, and starts the worker without the upstream
  web-host `--urls` argument. These are intentional local adaptations. The upstream
  `SimpleL7Proxy/Dockerfile`, sample deployment, scratch file, and build helper remain
  provenance-tracked but are excluded from AI4IA's Docker build context and are never built.

### Intentional source deviation

Twenty-four upstream files carry AI4IA security, correctness, dependency, or
telemetry patches over the audited pin; five of them are the CompanionApp
hosted-mode patches described in its section below. Eight additional files are
AI4IA-owned. The complete machine-readable list and reason for every deviation
lives in `upstream-provenance.json`; the behaviorally important proxy groups are:

- `SimpleL7Proxy/Config/AppConfigService.cs` applies the AI4IA-owned default-deny
  `Config/AppConfigKeyPolicy.cs` to every key it downloads, before the key is resolved. App
  Configuration can set only the refresh sentinel and two reviewed request limits, each within
  a reviewed range; everything else keeps its environment value. See
  [App Configuration key policy](#app-configuration-key-policy).
- `SimpleL7Proxy/Config/IncomingAuthValidator.cs` trims the `header=` value of `ValidateAuthConfig`
  and defaults it to `S7P-KEY` for the actual key lookup. Upstream now assigns the raw header, so
  AI4IA's `Ocp-Apim-Subscription-Key` ingress no longer depends on this line alone, but the
  normalization stays. The patch also fails startup for `oauth2`/`mixed` inbound mode until a
  trusted OIDC/JWKS signing-key source is implemented, rather than accepting unsigned JWTs. That
  behavior is still unfixed upstream at this pin.
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
- The default-absent `Proxy/NoReplayAttempt.cs` contract binds authenticated
  request metadata before stripping/profile changes and verifies exact
  body/model/path bytes before a one-shot send. `ProxyWorker.cs` prevents host
  fallback and retains a dedicated nonredirecting HTTP/1.1 client through the
  response body (`ProxyData.cs`); its diagnostic URI now uses the actual
  destination builder rather than a platform-dependent relative URI.
  `RequeueDelayWorker.cs` and `DTO/RequestDataDtoV1.cs` refuse bounded
  persistence/recovery, and `ProxyHelperUtils.cs` redacts internal attempt
  headers. The versioned boundary requires the exact `/ai4ia-attempts-v1`
  host prefix, no prefix stripping, scoped API-key auth and no host requeue.
  Its explicit `probe=/` selects a non-probing host; omission would activate
  the production loader's legacy echo probe. The existing Host1 probe is unchanged.
  A missing bounded host cannot fall back to the catch-all host/key. Missing
  markers on this path, including a recovered DTO, are refused before dispatch.
  The bounded path also refuses upstream's caller controls `S7P-Model-Override`,
  `S7PDEBUGBODY`, `S7PDEBUGSTREAM` and `S7P-Iterator`. When every matching circuit is open, the
  upstream iterator throws a delayed requeue before any attempt; `ProxyWorker.cs`
  turns that into a refusal for a bounded request instead of a requeue.
  Ordinary retry behavior is unchanged. This is source staging, not
  proof of deployed APIM compatibility; see
  [the typed authority boundary](../docs/hard-quota-admission.md#bounded-one-attempt-source-transport).

The remaining declared deviations update the Application Insights 3.x /
OpenTelemetry integration, remove unused parser runtime packages, and keep the
runtime NuGet graph locked. All undeclared source files are upstream-identical
after line-ending normalization.
Re-evaluate and drop the OAuth part of the `IncomingAuthValidator.cs` patch when
refreshing to an upstream commit that verifies inbound JWT signatures.

**Provenance validation (2026-09-26):** `upstream-provenance.json` records the
canonical LF SHA-256 of every upstream and local file plus the explicit AI4IA
patch list. Raw upstream hashes remain as evidence, but checkout-specific local
bytes never gate CI. `scripts/tests/test_proxy_provenance.py` fails for an
added, deleted, or semantically changed file that is not represented exactly.
The current measured breakdown is:

- **290 files** are content-equivalent to upstream after CRLF/LF canonicalization.
- **24 files** contain the documented AI4IA source patches.
- **8 files** are AI4IA additions: `Config/SecretComparer.cs`,
  `Config/AppConfigKeyPolicy.cs`, `Proxy/NoReplayAttempt.cs`,
  `CompanionApp/Ai4ia/HostedGuard.cs`, plus four `packages.lock.json` files used by
  the runtime project graphs.
- **95 upstream files** are deliberately not vendored (`ai4ia-excluded`). Each is
  recorded with its upstream hash under one declared exclusion rule and reason.
  Generation and `--check` fail if an excluded file appears locally, an
  unexcluded upstream file is missing, or a rule matches nothing.

The upstream tree has 409 files; the local scoped tree has 322, and the manifest
records all 417 paths. Regenerate only after fetching and reviewing the pinned
upstream commit:

```powershell
git fetch --no-tags https://github.com/microsoft/SimpleL7Proxy.git b0066b0e53f89abb5e84cfeacda2fdcaca8b081e
python scripts/gen-proxy-provenance.py --upstream-ref FETCH_HEAD
python scripts/gen-proxy-provenance.py --check
```

**Pin currency (measured 2026-09-25):** upstream `main` is exactly the pin. The
refresh from `d9eb1d1f…` absorbed 249 upstream commits and 74 changed files in
the three vendored projects. `Shared/` did not change. Thirteen of the previously
patched files changed upstream and were merged by hand. Upstream now ships the
failed App Configuration download guard itself, so AI4IA no longer patches that
guard; `Config/AppConfigService.cs` is patched again only for the key policy.

### Upstream behavior absorbed at this pin

These are the upstream changes a gateway operator must know about, and what
AI4IA does about each:

- **Host blocking on Retry-After.** The `retryafter=` host flag was inert at the
  previous pin. It now makes a host's circuit breaker honor `Retry-After` and
  `retry-after-ms` on any tracked failure, and it defaults to `true`. APIM's
  on-error path emits `retry-after-ms`, so one 5xx from one model would block the
  single catch-all host, and with it every model. Both authored hosts therefore
  set `retryafter=false`, preserving the previous behavior. Covered by
  `AI4IA.Proxy.Tests/GatewayUpstreamPolicyTests.cs`.
- **New caller controls.** `S7P-Model-Override` rewrites the body `model` and the
  `x-LLMModel` routing header, and `S7PDEBUGBODY` logs the full request body.
  `S7PDEBUGSTREAM` makes the token processor log up to the last ten response lines
  at Information. For a non-streaming completion that is the whole JSON, including
  `message.content`. The authored `DisallowedHeaders` policy removes all three
  after authentication and before the worker reads them
  (`IngressWorkerPolicyTests`, which runs the real listener and worker loop). The
  pre-existing `S7PDEBUG` still enables request debug logging, and it cannot be
  stripped because the listener reads it before the policy runs.
  `S7P-Iterator` is also parsed before that policy runs.
  It selects `SinglePass` or `MultiPass`. `MultiPass` reuses the single catch-all
  host lap after lap, up to `MaxAttempts`, so the authored `MaxAttempts=1` keeps it
  at one send (`CallerSelectedIterationIsBoundedByTheAuthoredMaxAttempts`). The API
  never sends any of these headers. A bounded request refuses all four upstream
  controls, and `S7PDEBUG` as well.
- **Iteration and retries.** Iterators were rewritten. The default `SinglePass`
  tries each matching host once per dispatch, and `MaxAttempts` now bounds only
  `MultiPass` (default 10). AI4IA's catch-all host still makes one attempt per
  dispatch. APIM's `429` + `S7PREQUEUE` delayed requeue is unchanged.
- **Open circuits requeue.** When every matching host's breaker is open, the
  iterator now throws a delayed requeue instead of skipping the host and failing
  fast. Ordinary requests wait for the breaker; bounded requests are refused.
- **Acceptable statuses.** `AcceptableStatusCodes` defaults to
  `[200,202,400,401,403,404,408,410,412,417]`. Those responses now return the
  backend's own body without failover or synthesizing "No active hosts". With one
  catch-all host the status code the API sees is unchanged, and the API does not
  parse proxy error bodies.
- **Backpressure.** Ingress 429s and readiness failures now start at 50% of the
  parent breaker's threshold rather than 100%. That parent counts only probe
  timeouts, and one probed host polled every 15 seconds cannot reach 25 timeouts
  in the 60-second window, so this is inert for AI4IA.
- **Configuration.** Staged backend configuration now swaps atomically and keeps
  the last good snapshot on error. `EVENT_LOGGERS` defaults to `none` instead of
  `file`, so no local event file is written unless Event Hub export sets
  `eventhub`. The new named `Path_*` routes, `prioritygroup`,
  `acceptablepriorities`, `mode=indirect`, profile rules, suspended users and
  `AuthProviders` are all inert unless configured, and AI4IA configures none of
  them. None of the keys AI4IA sets was renamed.
- **Telemetry.** Events gain requeue-delay, time-to-first-byte and
  `x-backend-label` fields, and `PolicyCycleCounter` was renamed
  `APIMPolicyCycleCounter` internally. The proxy-to-APIM header contract is
  unchanged.

The generated APIM policies remain derived from upstream's APIM Policy v3.0 at the
previous pin. They are a separate artifact from this source vendoring.
Regenerate the manifest whenever the pin, explicit patch list, or vendored file
contents change. For runtime dependency updates, first refresh both complete graphs
from the repository root, with
`dotnet restore proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --force-evaluate` and
`dotnet restore proxy/AI4IA.CompanionApp.Tests/AI4IA.CompanionApp.Tests.csproj --force-evaluate`,
and commit every changed lockfile. A referenced project's updated lock does not
automatically refresh either top-level test project's lock.

To refresh the vendored copy, check out the audited upstream commit and mirror the four project
directories from upstream `src/` (excluding `bin/`/`obj/`). For `CompanionApp/`, copy only files
that no `AI4IA_EXCLUSION_REASONS` rule in `scripts/gen-proxy-provenance.py` matches, then review
new upstream pages against the hosted-mode boundary before vendoring them. Keep this README and
the AI4IA Dockerfiles, reapply/test the documented source patches, verify every other source file
is byte-for-byte identical to upstream, and update both pin references.

### Recorded upstream findings (not patched)

- **Replica and revision are swapped in telemetry.** `ApplyConfigPlugin` in
  `Config/ConfigParser.cs` stores the plugin's `InstanceID` (`CONTAINER_APP_REVISION`) as
  `ReplicaName` and its `ConfigInstanceID` (`CONTAINER_APP_REPLICA_NAME`) as `Revision`. The
  plugin only became live at this pin, when upstream fixed a typo in the default
  `EnvPluginClass` type name. Since then the startup banner and every proxy event's
  `Replica` dimension (`Events/CommonEventHeaders.cs`) carry the revision name, so replicas
  of one revision cannot be told apart in telemetry. Request-ID prefixes are unaffected:
  they are built from the host name before the plugin runs.
- **Upstream's CompanionApp references a moved file.** At this pin, upstream's
  `CompanionApp.csproj` and CompanionApp `Dockerfile` still reference
  `deployment/deploy.parameters.example.sh`, which upstream commit `714b39f` moved to
  `deployment/interactive/`. AI4IA's vendored csproj does not embed that resource, and
  AI4IA builds its own `CompanionApp.Dockerfile`.
- **Circuit-breaker settings do not refresh live.** `Backend/CircuitBreaker.cs` copies
  `CBErrorThreshold` and `CBTimeslice` only in its constructor. A warm refresh changes the
  option but not a running breaker, so the value applies only to breakers built later, at a
  restart or scale-out. At a threshold of 1 or less, `GetBackpressureDelay()` reports
  backpressure with no failure recorded, and `server.cs` then answers every request with 429
  before authentication. The key policy refuses both keys.
- **A large default TTL expires every request.** `RequestData.CalculateExpiration` computes
  `DefaultTTLSecs * 1000` as an `int`. Above 2,147,483 seconds it overflows and every
  deadline is in the past. The key policy accepts at most 1,200 seconds.

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
  `host=<apim-gateway>;mode=apim;probe=/openai/status;processor=OpenAI;api-key-header=Ocp-Apim-Subscription-Key;retryafter=false`.
  The APIM subscription key is a Container App secret exposed only through `Host1-api-key`; it is
  never embedded in `Host1`.
- Separately scoped APIM APIs get their own specific-path hosts. A request whose path matches a
  specific host goes only to the matching specific hosts, never to the catch-all `Host1`. Numbered
  `HostN` entries are read only until the first gap, so an optional host that could follow an
  absent conditional `Host2` must be **named** (`Host-<name>` plus `Host-<name>-api-key`), which
  the loader reads from the environment separately. The default-off photo avatar surface uses
  `Host-photoavatars` (`path=/ai4ia-photo-avatars-v1;stripprefix=false;retryafter=false`, its own
  API-scoped key) for this reason; with `MaxAttempts=1`, single-pass iteration and no
  `S7PREQUEUE`, a create is sent at most once.
- APIM supplies `context.Api.Path` with a leading slash (`/ai4ia-photo-avatars-v1`), although
  the API's ARM `path` has none. The photo avatar and versioned API guards trim slashes before
  their exact comparison, and the offline APIM harness
  (`proxy/AI4IA.Proxy.Tests/ApimPolicyHarness.cs`) supplies the same form. A slashless
  comparison refused every photo avatar call in production on 2026-09-26, while the harness,
  which then supplied the slashless form, stayed green.
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

One matching host and the default `SinglePass` iteration prevent retry multiplication: APIM owns
immediate backend attempts inside one proxy dispatch; the proxy owns delayed requeue, queue TTL,
and its per-replica circuit breaker. `MaxAttempts=1` bounds `MultiPass`, the only mode it applies
to, so a caller-selected `S7P-Iterator: MultiPass` makes at most one lifetime attempt. Both hosts
set `retryafter=false`, so a host is blocked only when its breaker reaches the failure threshold.
While it is blocked, ordinary requests are requeued with a delay rather than failed.
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

- App Configuration is read with `id-proxy` for the sentinel-driven warm refresh, restricted
  by the [App Configuration key policy](#app-configuration-key-policy). Every other proxy
  setting, including Event Hub and async settings, comes from the Container App environment.
- Event Hub export is default-off and emits routing/status/latency metadata with
  request/response header logging disabled. It is not a work queue.
- Durable async is default-off and provisions dedicated MI-only Blob + Service Bus
  resources. It does not make the synchronous queue durable.
- Profiles are default-off and `UserConfigRequired=true` when enabled. The only
  supported source is the secret-mounted local snapshot. Validation blocks
  enablement until the edge derives a verified app identity; the proxy never
  reads Cosmos directly.

## App Configuration key policy

Upstream applies every downloaded `Warm:` and `Cold:` key, and an App Configuration value
overrides the Container App environment. Write access to the store would therefore be proxy
administration. A single write could:

- turn off inbound authentication (`Warm:Profiles:Auth:Config` set to
  `enabled=false;mode=none`), or add a second ingress key (`Profiles:Auth:Key2`);
- point `Host1` somewhere else. The replacement inherits the environment's `Host1-api-key`,
  the proxy's APIM model key;
- add `Path_*` routes with their own attempts and iteration mode, or name `AuthProviders`
  types that the proxy loads by reflection;
- rewrite the header strip, disallow and logging lists.

AI4IA authors all of that in `gateway.bicep`. The AI4IA-owned `Config/AppConfigKeyPolicy.cs`
is default-deny. `Config/AppConfigService.cs` applies it to every key and value it downloads,
at startup and on each warm refresh, before the key is resolved. Only these keys apply, and
each limit only within its range:

| App Configuration key | Proxy setting | Accepted values | Effect |
| --- | --- | --- | --- |
| `Warm:Sentinel` | `Sentinel` | Any | A change triggers the warm refresh |
| `Warm:Request:DefaultTimeout` | `Timeout` | `180000` to `1200000` | Milliseconds a backend attempt may wait for response headers |
| `Warm:Request:DefaultTTLSecs` | `DefaultTTLSecs` | `300` to `1200` | Seconds a request may spend queued and retried, from enqueue |

- Every other key keeps its environment value. That includes every `Cold:` key and any
  other prefix, all backend host and route keys, inbound authentication, the header and
  logging policy, the circuit-breaker settings, `LoadBalancing:*`, profiles, async,
  `Server:*` and unknown keys. `UseOAuth` and `OAuthAudience` are read only from the
  environment.
- A limit accepts only plain digits inside its range. Signs, spaces, decimals and the
  loader's arithmetic expressions are refused rather than interpreted. A refused value keeps
  the current setting: the environment value at startup, or the last accepted value on a
  refresh.
- The ranges follow from the deployed values. A backend attempt may wait for response headers
  until the earlier of the TTL deadline and now plus `Timeout`; the limit does not cover
  reading a streamed body. The API gives up on a proxied call after at most 180 seconds
  without a response (`gateway_image_timeout_seconds`), so the timeout floor never abandons a
  call the API is still waiting for. Bicep sets neither limit, so the deployed values are the
  proxy defaults: a 20-minute timeout and a 300-second TTL. App Configuration can shorten the
  timeout or lengthen the TTL, but neither beyond 20 minutes.
- The circuit-breaker settings are refused. A breaker reads them only when it is built, so a
  warm write does nothing until the next restart or scale-out. It then reaches the parent
  breaker, which gates all ingress before authentication: at `CBErrorThreshold=1`, every
  request gets 429 with no failure recorded. See
  [Recorded upstream findings](#recorded-upstream-findings-not-patched).
- Each download logs at most two warnings, one for refused keys and one for refused values.
  Each names at most 20 keys, each bounded and made printable. Values are never logged,
  because a refused value can be a credential.
- A store that holds only refused keys behaves like an empty store.
- Matching is exact: `Warm:` plus a reviewed key path, compared case-insensitively like the
  rest of the loader.
- Changing the allowlist or a range is a reviewed code change.
  `scripts/tests/test_proxy_delivery_contracts.py` fails if a reviewed key is also authored in
  `gateway.bicep`, so App Configuration can never override a Bicep setting. It also fails if
  the timeout floor drops below one of the API's gateway timeouts, or infra overrides one.
- The only writer today is `postprovision.ps1`, which reconciles `Warm:Sentinel=ready`
  through the OIDC deployment identity. That identity is the only one with App Configuration
  Data Owner; the proxy identity has only Data Reader. Never grant a write role to a runtime
  identity.
- `AI4IA.Proxy.Tests/AppConfigKeyPolicyTests.cs` and
  `IngressWorkerPolicyTests.AppConfigurationCannotTurnOffInboundAuthentication` drive the
  real download, bootstrap merge, backend registration, warm refresh and listener. Each
  refusal is paired with a control that runs the same write through upstream's download and
  shows it applying.

## CompanionApp telemetry console (optional)

`AI4IA_COMPANION_APP_ENABLED` (default `false`) hosts the vendored CompanionApp
subset as an admin-only, read-only view of this proxy's Event Hub telemetry. It
needs `AI4IA_PROXY_EVENTHUB_TELEMETRY_ENABLED=true`, which is a paid resource.
The operator procedure is in
[the feature runbook](../docs/runbooks/feature-enablement.md#companionapp-telemetry-console).

- **Retained:** the Event Hub monitor (`/eventhub`) and Insights (`/insights`),
  which parse the same event schema this pin emits.
- **Not vendored:** chat, the URL tester, stress, abort, investigator, vision,
  history, preferences, the App Configuration editor and deployment generation.
  They would send server-side requests to caller-chosen URLs with caller-chosen
  headers, create load and model cost, write shared history, or publish proxy
  configuration with the server identity. Each file is an `ai4ia-excluded`
  provenance entry. The subset ships only Bootstrap's minified stylesheet, because
  that is the only asset the retained pages load.
- **Patches:**
  - `Program.cs` puts an admin gate first in the pipeline. The gate re-checks the
    principal Container Apps authentication injects against the configured
    allowlist, and startup fails when that list is empty. It also replaces the DI
    `HttpClient` with one that refuses before connecting, and fails startup when an
    Event Hubs connection string or checkpoint store is configured, so managed
    identity is the only credential. It drops the App Configuration editor, chat
    stores and model presets. It removes upstream's fabricated "contoso" sample
    metrics, so nothing appears until real events arrive. It also lets the Data
    Protection key ring live outside the content root.
  - `CompanionApp.csproj` drops the embedded resources and content items of the
    excluded pages. It also references `Microsoft.AspNetCore.App.Internal.Assets`
    explicitly, at the runtime image's ASP.NET patch, because that package serves
    `_framework/blazor.web.js`. The Web SDK would otherwise add it implicitly, but
    only when `.razor` files exist at restore time, which the Docker restore layer
    lacks, and only at the SDK's own bundled patch. That would make a locked
    restore depend on layering and on the SDK version.
  - `Home.razor` and `NavMenu.razor` link only the retained pages.
  - `EventHubReader.cs` has two changes:
    - It serializes the pipeline. Upstream reads every partition concurrently and
      mutates shared request dictionaries without a lock, so a four-partition hub
      corrupted them and dropped events.
    - It no longer appends the raw JSON of unlabeled backend attempts to an unbounded
      `incomplete.json`. That JSON carries user id, path and backend hosts, and only
      the excluded `/incomplete` page read it.
  - The AI4IA-owned guard is `Ai4ia/HostedGuard.cs`.
  - `AI4IA.CompanionApp.Tests` drives the real host. Each of these checks runs
    against a control:
    - the admin gate;
    - the refused empty allowlist;
    - the compiled route allowlist, and the 404s for every upstream tool route;
    - the refused outbound request;
    - the empty startup catalog;
    - the refused shared-access secrets.
- **Image:** `CompanionApp.Dockerfile` reuses this proxy's digest-pinned bases, its
  locked restore and the build context's recursive `**/.env*` exclusion. It lays the
  application down as root and runs as the non-root app user. The only path the app
  user owns is the ephemeral key ring. The PR image job proves this on the built
  image by exporting its filesystem, because the chiseled runtime has no shell, and
  checking it with `scripts/check-image-ownership.py`.
- **Not an azd service:** the manual `companion-image.yml` workflow promotes an
  attested digest, and deploy.yml re-verifies that digest before provisioning.

## Build and supply-chain verification

CI restores the four proxy projects and the two CompanionApp projects from
checked-in NuGet locks, builds and tests them, and CodeQL analyzes the C# source.
The PR image build resolves both digest-pinned base images, then scans the final
loaded proxy and CompanionApp images for HIGH/CRITICAL findings using the
CVE-specific `proxy/.trivyignore` policy. It retains SPDX SBOMs, plus the proxy's
build metadata, for 30 days.

Repository secret/config scans also cover the vendored tree. Their only
upstream exceptions are exact-file/fingerprint entries whose paths are checked
against `upstream-provenance.json` as unpatched blobs.

The PR build evidence is deliberately marked unsigned and is never deployed.
Production images are signed separately. deploy.yml attests and verifies the
web, api and proxy digests, and `companion-image.yml` does the same for the
optional CompanionApp image. See
[the release runbook](../docs/runbooks/deployment.md#production-image-attestations).

## Current scope

- SimpleL7Proxy + APIM front the Foundry endpoints with a `models.json`-derived
  allowlist, auth, routing, request IDs, multi-region selection, queueing/requeue,
  and App Insights. Priority reservations, Event Hub export, durable async, and
  profiles are optional and default off.
- Custom domain: `genaiproxy.nomad-analytics.com` (public, governed).
