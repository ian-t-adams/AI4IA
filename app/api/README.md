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

## Voice Live (Phase 10) — real-time speech-to-speech relay

A **feature-flagged, default-OFF** governed WebSocket relay at `WebSocket
/api/voice/live` (`routers/realtime.py`). With `AI4IA_REALTIME_ENABLED=false` (the
default) the route refuses immediately, so the API is inert and unchanged. The
existing turn-based REST voice (`routers/voice.py`: transcription + speech) is
distinct and untouched.

When enabled, the browser connects **directly to the API's external ingress** (the
web HTTP proxy can't proxy WebSockets) and the relay:

1. refuses if the feature flag is off;
2. validates the browser `Origin` against `AI4IA_REALTIME_ALLOWED_ORIGINS` (WS
   handshakes aren't CORS-preflighted, so the relay checks Origin itself; empty
   reflects only in `local`, else rejects — fail-closed);
3. extracts + validates the caller's token from a **WebSocket subprotocol**
   (`ai4ia-bearer` for Entra, `ai4ia-dev` for dev) via the auth provider directly,
   since a browser-direct WS can't set `Authorization` / be proxy-stamped;
4. resolves the realtime deployment from the catalog (browser never sees it);
5. runs the **entitlement gate** before opening the upstream socket;
6. opens the upstream Azure realtime WS through the **same model gateway** + key as
   chat, **meters one unknown call** per session (`session_id="voice-live"`), then
   pumps text+binary frames both ways until either side closes (with an optional
   `AI4IA_REALTIME_MAX_SESSION_SECONDS` clamp).

The event protocol stays client-driven (the relay is a mostly-transparent pump);
the relay owns only the connection, governance, and metering. See `.env.example`
for the `AI4IA_REALTIME_*` settings.
