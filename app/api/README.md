# app/api — AI4IA Backend (Python FastAPI)

The application backend: auth, sessions, chat, agents, memory, tools, and the model-gateway
client. **All model calls go through the gateway** (`/proxy`), never directly to Foundry.

## Responsibilities (phased)
- **Phase 2:** MSAL auth + B2B invite/onboarding; canonical internal user ID (decoupled from
  Entra OID) + IdP mapping; chat endpoint via the model gateway; per-user sessions/history in
  Cosmos; model picker + parameters; per-user/per-model request + token limits; feature flags.
- **Phase 4:** MAF agents + workflows; Foundry toolbox + BYO MCP tools; custom tools behind a
  tool-safety registry (allowlist, scopes, per-tool secrets, human-approval for destructive
  actions); agent-step tracing + a small regression eval set.
- **Phase 5:** mem0 + pgvector memory; erase/clear (tombstone + async purge + verify); long-chat
  summarize & continue.

## Key principles
- Cosmos DB is canonical for sessions/messages/agents/workflows; memory stores are rebuildable.
- Secrets come from Key Vault / App Configuration via managed identity — never from code.
- Correlation IDs propagate to the gateway and into traces.

## Stack
- FastAPI + Pydantic, `azure-identity`, MAF (`agent-framework`), Cosmos + Postgres clients.
  Packaged with `pyproject.toml`; containerized (`Dockerfile`) → Azure Container Apps.

## Local dev (to be added in Phase 2)
```
python -m venv .venv && . .venv/Scripts/Activate.ps1
pip install -e ".[dev]"
uvicorn ai4ia_api.main:app --reload
```
