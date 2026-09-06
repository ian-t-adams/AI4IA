# app/api — AI4IA Backend

FastAPI service for auth, sessions, chat, agents, tools, memory, documents,
usage, admin analytics, and model-gateway access. Model calls use the configured
gateway except for Azure service control/data planes that are not OpenAI chat
surfaces.

`AI4IA_MODEL_GATEWAY_URL` is the SimpleL7Proxy `/openai` URL for compatible
HTTP/SSE calls. When Voice Live is enabled, `AI4IA_REALTIME_BASE_URL` is the APIM
`/openai` URL used by the server-side WebSocket relay; it intentionally bypasses
SimpleL7Proxy.

## Responsibilities

- Auth: dev mode for local work, Entra validation for deployed environments,
  canonical internal user ids, and admin gates.
- Chat: sessions/history, streaming persistence, model selection, per-model token
  caps, optional rolling summarization, standing and one-turn agent routing,
  conversation tool/document selections, inspector snapshots, and slash commands.
- Agents/tools: curated agents, user-defined agents, workflows, governed built-in
  tools, generated image/video/document artifacts, Web IQ search tools when
  enabled, and user-registered remote MCP servers. The default-off
  `AI4IA_TOOL_AUTO_APPROVE_ENABLED` gate permits explicit session/run consent for
  enabled tools without bypassing execution authorization or losing activity and
  receipt evidence.
- Memory: disabled/in-memory/Cosmos backends; catalog-driven planning and
  embeddings; automatic recall; owner-scoped create/edit/delete; concurrency-safe
  forget; and atomic document memory replacement.
- Documents: per-session attachments plus the feature-gated cross-session library,
  Content Understanding ingest, retrieval, code interpreter, annotations, sharing,
  media playback metadata, and processing/export tools.
- Operations: usage ledger, entitlements, admin usage rollups, Azure Monitor
  resource panels, fixed bounded Log Analytics operations/security queries,
  structured metadata events, correlation ids, and Application Insights export
  when configured.

## Local dev

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
uvicorn ai4ia_api.main:app --reload
```

Run checks from this folder:

```powershell
ruff check .
pytest -q
```

The container image runs as non-root UID `10001`. Azure Container Apps is the
health authority: Bicep wires process-only `/health/live` liveness and cached
session-store `/health/ready` readiness probes. The image does not declare a
second Docker health policy that ACA ignores.

## Configuration posture

Feature flags are fail-closed in `ai4ia_api.config.Settings.validate_runtime`.
Local can use in-memory stores and fake clients; deployed environments must wire
durable stores, credentials, Origin allowlists, and real auth for the features
they enable. The authoritative flag list is
[`../../docs/runbooks/feature-enablement.md`](../../docs/runbooks/feature-enablement.md).

## Current gaps

- Memory has no global user-facing consent/toggle.
- Custom analyzer authoring is not surfaced.
