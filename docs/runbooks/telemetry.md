# Telemetry and admin diagnostics

AI4IA's admin dashboard is read-only over existing usage, Application Insights,
Log Analytics, and Azure Monitor data. It provisions no monitoring resource and
never exposes prompts, document bodies, tool arguments/results, audio, or
transcripts.

## Sources

| View | Source | Freshness and unknown behavior |
|---|---|---|
| Requests, errors, dependencies | FastAPI/OpenTelemetry and instrumented `httpx` | Depends on Application Insights export; unavailable is not zero |
| GenAI text-model calls | Content-free gateway model spans | Actual adapted Chat Completions, Responses and Claude calls; missing usage/response identity stays unknown |
| Tokens and known cost | Per-user Cosmos usage ledger | Missing provider usage or price is counted as unknown |
| Voice Live | `voice_live_completion` metadata event and usage ledger | Provider/model/outcome/close/frame metadata only |
| MCP tools | Redacted structured MCP events | Process/log export availability controls freshness |
| Document ingest | `document_ingest` receipt plus `document_ingest_terminal` enrichment events | Terminal ready/failed/cancelled, modality, bounded stage, persistence outcome, and duration only |
| Memory | `memory_operation` events for list/delete/recall/save | Operation/status/backend/count/latency only; no memory text or id |
| Security blocks | `security_block` custom events in `AppEvents` | Bounded category/reason/source for HTTP/admin auth, tool authorization, SSRF, and realtime denial |
| Platform resources | Azure Monitor Metrics | One-hour window; `—` means no datapoint |

Azure Monitor's batch endpoint must match the resource's region. API and Cosmos
use `AI4IA_METRICS_ENDPOINT`; Search receives
`AI4IA_METRICS_SEARCH_ENDPOINT`, derived from the same location used to deploy
Search. The service reuses clients for identical endpoints. A Search service in
East US must not be queried through the API's East US 2 endpoint; no resource
move or additional role is needed to correct that routing.

## Querying it by hand (incident response)

The deployed Application Insights component is workspace-based. Query the
Log Analytics workspace and its table names directly; do not interpret an empty
query against a different API/schema as a clean bill of health.

| Classic name | Workspace table |
| --- | --- |
| `customEvents` | `AppEvents` |
| `traces` | `AppTraces` |
| `requests` | `AppRequests` |
| `dependencies` | `AppDependencies` |
| `exceptions` | `AppExceptions` |

```powershell
$cid = az monitor log-analytics workspace show -g <rg> -n <workspace> --query customerId -o tsv
az monitor log-analytics query -w $cid --analytics-query "AppEvents | where TimeGenerated > ago(24h) | summarize count() by Name"
```

**Check source coverage before interpreting absence.** Count rows from the same
producer and time window before applying an incident predicate. Zero matches in
a table receiving no events proves nothing. Verify the proxy's configured
exporters rather than assuming its events reach Application Insights.
Container stdout is queried separately in `ContainerAppConsoleLogs_CL`, filtered
by `ContainerAppName_s` for the target proxy.

## Admin API contract

- `GET /api/admin/metrics/operations?minutes=15..1440`
- `GET /api/admin/metrics/security?minutes=15..1440`

Both routes require application admin authorization. The server chooses every KQL
query; callers can only choose the bounded time window.

Each panel returns `source`, `generatedAt`, `sourceTimestamp`, `lagSeconds`,
`status` (`ok`, `partial`, `stale`, or `unavailable`), `reason`, and bounded rows.
No rows is rendered as no matching telemetry, never as a numeric zero.
Document panels query terminal enrichment events rather than upload receipts. Memory
and security panels become `partial` when expected producer categories are absent;
they do not infer successful zero-failure operation from missing events. Security
queries use the deployed custom-event `AppEvents` table, not general trace-message
search.
`ready` is emitted only after an atomic ingest-owned manifest patch commits and the
stored status is confirmed. ACLs, visibility, annotations, versions, and other
owner-controlled fields are never part of that patch; CAS retries merge only the
ingest-owned status/output fields.
Startup recovery uses the same conditional patch and applies only while the stored
status is still `analyzing`; a concurrent completion or owner metadata/access change
is preserved rather than overwritten by the recovery snapshot.

## Privacy and cardinality

- Keep event names and dimensions stable and low-cardinality.
- Do not add user message, prompt, transcript, document text, tool payload, URL
  or host, memory text/id, document filename/id, user identity, secrets, credentials,
  or raw exception bodies.
- Usage ledger keys never enter logs or custom events. `chat_completion` custom
  events use stable, domain-separated SHA-256 prefixes (`userHash` /
  `sessionHash`) for correlation. The usage service emits no model-usage payload
  to general container stdout. Raw internal ids remain in the owner-scoped Cosmos
  ledger only. Admin directory enrichment is a separate, explicitly enabled
  admin-plane lookup.
- Correlation ids may cross API, SimpleL7Proxy, APIM, and Foundry; they are not
  credentials.

## Request spans and application factory wiring

The API explicitly instruments the **actual application instance** with
`FastAPIInstrumentor.instrument_app` after registering its routes and middleware.
The Azure distro's global FastAPI-class replacement is disabled. Replacing
`fastapi.FastAPI` cannot instrument a constructor that `main.py` already imported;
calling the distro before constructing that prebound class is not sufficient.
The factory uses the canonical `fastapi.applications.FastAPI` class so an ambient
class replacement cannot silently double-instrument or bypass the per-app gate.

Azure Monitor 1.8.10 also enables both `httpx` and `httpx2` instrumentor
entrypoints by default. Both are explicitly disabled in the distro options:
`logging_setup` retains the existing manual HTTPX owner, and HTTPX2 is not
opted in. Otherwise the distro takes ownership first and the application's
second attempt is rejected as already instrumented. Keeping a single owner
does not disable the existing outbound dependency spans or their metrics.

Request instrumentation requires both this app's nonempty Application Insights
connection setting and successful existing exporter configuration. Repeated
factory calls reuse that exporter; each enabled app is instrumented once.
An app without the setting remains uninstrumented even if another app in the
process has configured the exporter. The instrumentor's process-wide
`BackgroundTask` hook cannot create non-request spans through this facade;
otherwise the first enabled app's tracer would leak into a disabled app's
background responses. Tasks still execute in their existing context.
Health/auth responses, middleware order,
correlation-header echo and asynchronous lifespan cleanup retain their existing
application behavior.

The shipping FastAPI instrumentor records raw request metadata and explicit
exception events by default, which would broaden the app's no-content posture.
A narrowly scoped public OTel tracer/span facade therefore projects this
instrumentor's writes **before** they reach the existing SDK. It delegates to the
already-configured provider; it does not register another SDK provider, sampler,
processor or exporter. Captured fields are registered route templates, bounded
method/protocol/scheme/status values and a fixed error category. Unknown routes
use a method-only name. Raw path/query strings, host/peer addresses, user agents,
headers, incoming correlation strings, bodies, identities, links, events and
exception descriptions are not exported by this producer. Numeric W3C trace
correlation and flags remain; caller-written remote tracestate/baggage is not
copied into request or model span metadata. The existing correlation header still
reaches the application/response; it is not treated as trusted span content.

ASGI receive/send spans are excluded. This request instrumentation uses a local
no-op meter rather than start an additional request-metrics feed whose raw
host/path dimensions bypass the span projection. Other existing metric and event
producers keep their previous configuration and allowlists.

**Sampling is unchanged.** Azure Monitor 1.8.10 selects its rate-limited sampler
by default when no explicit sampling setting is supplied. The request facade
retains the actual SDK parent context and sampling attributes used by
exporter 1.0.0b57 / SDK 1.44.0. Its local-parent rules include dropping children
of a dropped parent and inheriting an explicitly recorded sample rate; a recorded
100%-rate parent with no explicit rate attribute may be sampled again by b57.
No always-on override, sample-rate increase or ingestion-limit change is made.
Missing request spans can affect parentage, but a bounded observation with zero
GenAI records does not establish an all-time exporter failure or prove sampling
as the cause.

`app/api/tests/test_fastapi_telemetry.py` executes the real `create_app`, Azure
configuration, shipping instrumentor and SDK in clean subprocesses, substituting
only the network exporter with an in-memory exporter and provider HTTP with
synthetic transports. It checks whole request/GenAI spans and the installed Azure
envelope conversion, one-time configuration, per-app gates, preserved auth/errors/
cleanup, real gateway child-parent links, poisoned fields and actual sampler
controls. Imported distro, exporter, sampler, SDK and instrumentor source files
must match their installed wheel RECORD hashes, not just package metadata or
paths. The 1.8.10 / b57 / 1.44.0 / 0.65b0 train was installed with the public
lock's artifact hashes enforced. Wheel requirements and the official 1.8.10
tag's `setup.py` require b57, despite the changelog's b56 statement.

Paired controls remove only the HTTPX ownership opt-outs and prove that the
real distro then loads both entrypoints and attempts HTTPX instrumentation
before the application. With the fix, it loads neither and the application
instruments once. Sync and async HTTPX transports still emit their ordinary
dependency spans and metrics, while actual gateway calls through that same
global wrapper emit only the content-free GenAI span, with no raw HTTP metrics.
The HTTPX2 control proves entrypoint selection, not execution of an uninstalled
HTTPX2 client.

Offline SDK tests explicitly disable its control-plane worker and deny the
`requests` transport as well as substituting provider HTTP: b57's distro can
start a background configuration worker even with an in-memory exporter.
These are offline controls, not production request/GenAI export evidence.

After an approved deployment, verify the exact serving API image first. The
parent/operator can then compare bounded, content-free request/dependency and
GenAI coverage in the **same** known Application Insights resource and time
window, under unchanged sampling, without collecting bodies or enabling a live
evaluation actor. A missing source or zero records remains incomplete evidence,
not a reason to activate a paid probe or force sampling.

## Content-free GenAI model spans

`ai4ia_api.genai` contract 1.0.0 uses the existing connection-gated Azure Monitor
exporter. No separate exporter, SDK integration, telemetry resource or permission
is introduced. The current official GenAI conventions have moved to the
[OpenTelemetry GenAI repository](https://github.com/open-telemetry/semantic-conventions-genai/blob/0c87594975195608dc91b3f702e250a7b240c151/docs/gen-ai/gen-ai-spans.md).
The exact development revision
`0c87594975195608dc91b3f702e250a7b240c151` is pinned in the span contract. It is
not a stable release or a published schema URL. The compatible Azure Monitor
1.8.10 / HTTPX 0.65b0 dependency upgrade preserves this semantic contract;
dependency updates do not authorize new constants, capture or payload fields.

Each logical text-model call emits one CLIENT span. Its timestamps measure the
operation through response completion, stream end, failure or cancellation.
The existing Chat Completions stream-options retry remains within the same
logical span; repeated cumulative token chunks replace counts, never add them.
Child calls have their own spans, not duplicated parent token totals. The usage
ledger and existing custom events are separate evidence, not extra model calls.

The allowlist is operation/provider name, catalog-owned request/observed response
model identifiers, actual post-admission adapted scalar controls, native enum
finish reasons, validated input/output token counts, HTTP attempt count, coverage,
the convention revision and a fixed error category. Provider name identifies the
wire-adapter family, not a discovered Azure resource or billable publisher.
Responses use their returned completion status or incomplete reason rather than
inventing a model-internal decision. Unknown response model strings are omitted,
not regex-sanitized into identifiers. Failed/truncated streams never turn missing
usage into zero or export stale partial totals.

The installed Azure exporter still classifies GenAI dependencies through the
deprecated `gen_ai.system` attribute. A fixed compatibility alias equals
`gen_ai.provider.name`; the alias does not add a span or content. Capture tests
exercise both complete SDK spans and the installed exporter's actual Azure
envelope conversion, without starting a network exporter.

No prompt/completion/tool/schema/stop-sequence payload, URL/host, conversation id,
user identity, baggage, exception event, exception message or status description
is added. Duplicate automatic HTTPX dependency spans on these calls are
suppressed, so URL/exception capture settings cannot reintroduce their content.
No instrumentation context is kept across generator yields; ASGI disconnect
cleanup may run in a different task. Content-capture environment variables do not
broaden this explicit projection. Media/embeddings/realtime are not claimed as
covered by this text-model span contract.

This is an application-owned projection following the documented
[Azure Monitor custom-telemetry path](https://learn.microsoft.com/en-us/azure/azure-monitor/app/opentelemetry-add-modify?tabs=python),
not wholesale framework instrumentation. Framework documentation may demonstrate
[opt-in content recording](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-framework);
those examples are not authorization to enable it in AI4IA.

## Behavioral evaluations are not production telemetry

The [offline behavioral evaluator](../behavioral-evaluations.md) exercises real
application seams with synthetic HTTP/provider fixtures. It exports no spans or
production traces. Its local/CI report contains version identifiers, check
outcomes and synthetic counters only, not receipt payloads or identities.
Fixture latency and token prices are not measured live model quality or billing.

The separate default-off live authored-synthetic driver, activation variables,
finite budgets and exact-owner cleanup policy are documented in the
[evaluation guide](../behavioral-evaluations.md#separate-opt-in-live-authored-tasks).
It is not a production-trace reader or a required PR gate. A paid judge and
production-content ingestion remain disabled. No `AppGenAIContent` routing,
RBAC, retention or consent policy is established by source tests or by running
either evaluation suite. Keep the telemetry compatibility pair and no-content
export contract intact.

## Diagnosing unavailable panels

1. Confirm the feature or resource-metrics flag is enabled.
2. Confirm the existing resource id is configured.
3. Confirm the API identity can read the existing metric source.
4. Check the panel detail and generated time before interpreting a blank value.
5. Use the linked Azure diagnostics experience for raw investigation.

## Explicitly unsupported

The current first release does not claim exact SimpleL7Proxy admission queue depth,
profile fairness, requeue, or circuit-breaker panels because the pinned proxy does
not expose stable queryable event dimensions for all of them. Quota forecasting,
shared realtime presence, and Workbooks remain backlog. A baseline **is deployed**:
API Container App `Requests` filtered to 5xx and Cosmos `TotalRequests` filtered
to 429, both severity 2, email the configured alert recipient. Additional APIM,
CU, no-ready-replica, and synthetic-path alerting remains open. Do not infer zero
for any unsupported dimension.

Do not add RBAC, a workspace, alerts, or a workbook during incident response without
separate approval and an infrastructure what-if.
