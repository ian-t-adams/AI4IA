# app/api — AI4IA Backend

FastAPI service for auth, sessions, chat, agents, tools, memory, documents,
usage, admin analytics, and model-gateway access. Model calls use the configured
gateway except for Azure service control/data planes that are not OpenAI chat
surfaces.

## Responsibilities

- Auth: dev mode for local work, Entra validation for deployed environments,
  canonical internal user ids, and admin gates.
- Chat: sessions/history, streaming persistence, model selection, per-model token
  caps, optional rolling summarization, `@agent` routing, and slash commands.
- Agents/tools: curated agents, user-defined agents, workflows, governed built-in
  tools, generated image/video/document artifacts, Web IQ search tools when
  enabled, and user-registered remote MCP servers.
- Memory: disabled/in-memory/pgvector/mem0 backends, automatic recall, explicit
  forget, and document save/forget.
- Documents: per-session attachments plus the feature-gated cross-session library,
  Content Understanding ingest, retrieval, code interpreter, annotations, sharing,
  media playback metadata, and processing/export tools.
- Operations: usage ledger, entitlements, admin usage rollups, Azure Monitor
  resource panels, structured logs, correlation ids, and Application Insights
  export when configured.

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

The container image runs as non-root UID `10001` and declares a Docker
`HEALTHCHECK` against `/health/live`. Azure Container Apps probe wiring still
lives in Bicep, but local Docker runs now have a basic liveness signal too.

## Configuration posture

Feature flags are fail-closed in `ai4ia_api.config.Settings.validate_runtime`.
Local can use in-memory stores and fake clients; deployed environments must wire
durable stores, credentials, Origin allowlists, and real auth for the features
they enable. The authoritative flag list is
[`../../docs/runbooks/feature-enablement.md`](../../docs/runbooks/feature-enablement.md).

## Current gaps

- Web IQ and inline-attachment Code Interpreter are implemented but disabled in
  the checked-in live parameters.
- Memory has no global user-facing consent/toggle or recalled-memory indicator.
- Custom analyzer authoring and non-document library upload UI are not surfaced.
