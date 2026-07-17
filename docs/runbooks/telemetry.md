# Telemetry and admin diagnostics

AI4IA's admin dashboard is read-only over existing usage, Application Insights,
Log Analytics, and Azure Monitor data. It provisions no monitoring resource and
never exposes prompts, document bodies, tool arguments/results, audio, or
transcripts.

## Sources

| View | Source | Freshness and unknown behavior |
|---|---|---|
| Requests, errors, dependencies | FastAPI/OpenTelemetry and instrumented `httpx` | Depends on Application Insights export; unavailable is not zero |
| Tokens and known cost | Per-user Cosmos usage ledger | Missing provider usage or price is counted as unknown |
| Voice Live | `voice_live_completion` metadata event and usage ledger | Provider/model/outcome/close/frame metadata only |
| MCP tools | Redacted structured MCP events | Process/log export availability controls freshness |
| Document ingest | `document_ingest` receipt plus `document_ingest_terminal` enrichment events | Terminal ready/failed/cancelled, modality, bounded stage, persistence outcome, and duration only |
| Memory | `memory_operation` events for list/delete/recall/save | Operation/status/backend/count/latency only; no memory text or id |
| Security blocks | `security_block` custom events in `AppEvents` | Bounded category/reason/source for HTTP/admin auth, tool authorization, SSRF, and realtime denial |
| Platform resources | Azure Monitor Metrics | One-hour window; `—` means no datapoint |

The admin API exposes:

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

## Privacy and cardinality

- Keep event names and dimensions stable and low-cardinality.
- Do not add user message, prompt, transcript, document text, tool payload, URL
  or host, memory text/id, document filename/id, user identity, secrets, credentials,
  or raw exception bodies.
- User identifiers remain internal ids and are shown as hashes unless an admin
  explicitly enables directory enrichment.
- Correlation ids may cross API, SimpleL7Proxy, APIM, and Foundry; they are not
  credentials.

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
shared realtime presence, Workbooks, and alert rules also remain backlog. Do not
infer zero for any unsupported dimension.

Do not add RBAC, a workspace, alerts, or a workbook during incident response without
separate approval and an infrastructure what-if.
