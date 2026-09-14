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

## Content-free GenAI model spans

`ai4ia_api.genai` contract 1.0.0 uses the existing connection-gated Azure Monitor
exporter. No separate exporter, SDK integration, telemetry resource or permission
is introduced. The current official GenAI conventions have moved to the
[OpenTelemetry GenAI repository](https://github.com/open-telemetry/semantic-conventions-genai/blob/0c87594975195608dc91b3f702e250a7b240c151/docs/gen-ai/gen-ai-spans.md).
The exact development revision
`0c87594975195608dc91b3f702e250a7b240c151` is pinned in the span contract. It is
not a stable release or a published schema URL. Do not upgrade the compatible
Azure Monitor 1.8.9 / HTTPX 0.64b0 dependency train merely to acquire new constants.

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
