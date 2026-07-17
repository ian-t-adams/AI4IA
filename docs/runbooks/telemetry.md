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
| Document ingest | `document_ingest` metadata event and library manifests | Status/modality/type/size only |
| Memory | `memory_list` and `memory_delete` metadata events | No memory text is emitted |
| Platform resources | Azure Monitor Metrics | One-hour window; `—` means no datapoint |

## Privacy and cardinality

- Keep event names and dimensions stable and low-cardinality.
- Do not add user message, prompt, transcript, document text, tool payload, URL
  secrets, credentials, or raw exception bodies.
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

Do not add RBAC, a workspace, alerts, or a workbook during incident response without
separate approval and an infrastructure what-if.
