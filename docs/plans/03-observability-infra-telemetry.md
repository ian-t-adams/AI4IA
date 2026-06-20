# WS3 — Observability infrastructure + telemetry export

**Goal:** get logs/metrics from **every** Azure resource into the central Log
Analytics workspace, and have the API export traces/metrics/custom events to
Application Insights. This is the data-plumbing half of "admin mode" — WS4
consumes it.

## Current state (verified against `main`)

- ✅ **Central monitoring provisioned:** `infra/modules/monitoring.bicep` creates a
  Log Analytics workspace, an Application Insights component (workspace-linked),
  and an Azure Monitor (Prometheus) workspace.
- ✅ **Container Apps logs flow** to Log Analytics via the managed-environment
  `appLogsConfiguration` (`infra/modules/containerapps.bicep`).
- ✅ **App Insights connection string is already injected** into the API container
  (`infra/modules/api.bicep`; `APPLICATIONINSIGHTS_CONNECTION_STRING` in
  `.env.example`).
- ❌ **No `Microsoft.Insights/diagnosticSettings`** anywhere in `infra/` — Cosmos,
  Postgres, AI Search, Storage, Event Hubs, Key Vault, and the OpenAI/Foundry
  account send nothing to Log Analytics.
- ❌ **No App Insights / OpenTelemetry SDK** in the API (`pyproject.toml` has no
  `azure-monitor-*` / `opentelemetry-*`); only stdout structured logging +
  correlation IDs (`logging_setup.py`). No traces/metrics/custom events exported.

## Target

### A. Diagnostic settings on every resource (infra)

- Add a small reusable pattern (inline per module, or a shared
  `infra/modules/diagnostics.bicep` helper) that wires
  `Microsoft.Insights/diagnosticSettings` → the existing Log Analytics workspace
  for: **Cosmos DB, Postgres Flexible Server, Azure AI Search, Storage (blob),
  Event Hubs namespace, Key Vault, and the Azure OpenAI / AI Foundry account**.
  Send the resource's supported log categories + `AllMetrics`.
- Pass the Log Analytics workspace id from `monitoring.bicep` into each module that
  needs it (thread through `main.bicep`).
- Keep retention aligned with the workspace (30-day) and avoid enabling
  high-volume categories that aren't useful (be deliberate, not "all logs on").

### B. App telemetry export (backend)

- Add `azure-monitor-opentelemetry` (the distro) to `app/api/pyproject.toml`.
- Initialize it once at app startup (in `main.py` lifespan or `logging_setup.py`)
  **only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set** — so local/dev with
  no connection string is a no-op. Instrument FastAPI + outbound HTTP so requests,
  dependencies, and exceptions flow to App Insights, preserving the existing
  correlation-id context.
- Emit **custom usage events/metrics** at the point where the usage ledger is
  written (`usage/service.py` `record_completion`) — e.g. a `chat_completion`
  custom event/metric carrying model, agent, token counts, cost, status. This is
  what makes per-model / per-user token analytics queryable in App Insights/KQL in
  addition to the Cosmos ledger.

### C. (Optional, future) activate the dormant Event Hubs telemetry hub

- The `telemetry` Event Hub + sender/receiver roles already exist but are unused.
  Out of scope for the first PR; note it as a fast-follow if a streaming pipeline
  is wanted. Do **not** build it speculatively.

## Files to change

- `infra/modules/*.bicep` — add `diagnosticSettings` to data/search/eventhubs/
  keyvault/openai modules; thread the workspace id.
- `infra/main.bicep` — pass the Log Analytics workspace id to those modules.
- `app/api/pyproject.toml` — add `azure-monitor-opentelemetry`.
- `app/api/src/ai4ia_api/main.py` and/or `logging_setup.py` — conditional telemetry
  init (no-op without a connection string).
- `app/api/src/ai4ia_api/usage/service.py` — emit a custom usage event/metric.
- `app/api/.env.example` — document any new telemetry toggles.

## Default-OFF / safety posture

- Telemetry init is a **no-op when no connection string is present** (local/dev
  unaffected), so this is inert in environments that don't opt in. Live already has
  the connection string, so this lights up telemetry there once deployed.
- Diagnostic settings only **add** monitoring; they don't change app behavior.
- Be cost-aware: pick categories deliberately; don't firehose verbose logs.

## Tests

- Infra: `az bicep build -f infra/main.bicep` exit 0; lint clean. Optionally a
  what-if/parameter check that each resource now has a diagnosticSettings child.
- Backend: telemetry init is skipped (no error) when the connection string is
  unset; the usage event is emitted (mock the exporter) when a completion is
  recorded; `record_completion` remains best-effort (never raises).

## Acceptance criteria

- After deploy, Log Analytics receives logs/metrics from Cosmos, Postgres, AI
  Search, Storage, Event Hubs, Key Vault, and OpenAI/Foundry.
- App Insights shows API request traces, dependencies, exceptions, and
  `chat_completion` usage events with model/token/cost dimensions.
- Local/dev runs unchanged (no connection string ⇒ no telemetry).
- `ruff check .` clean; `pytest -q` green except the known Windows flakes;
  `az bicep build` exit 0.
