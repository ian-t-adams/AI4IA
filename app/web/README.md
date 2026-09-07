# app/web — AI4IA Web

Next.js App Router frontend for chat, agents, workflows, voice, document library,
generated media, MCP server management, and the admin dashboard.

## Responsibilities

- Chat shell: sessions, streaming messages, responsive Conversation Inspector,
  catalog model controls, one authoritative prompt, governed agent/tool selection,
  document context, memory, usage, voice, and command affordances.
- Auth: runtime-selected dev or Entra/MSAL mode. The API remains the enforcement
  boundary; the web app only acquires and forwards tokens.
- Voice: feature-gated Voice Live over the API WebSocket relay. Provider/audio
  settings apply to the next connection; the API injects the selected agent or
  conversation prompt and governed tools.
- Library/media: cross-session document library, ingest status, sharing,
  annotations, save/forget memory actions, audio/video player, and citation
  deep-links.
- Tools: agent builder, workflow builder, imagery/video output rendering, custom
  MCP server management, and Web IQ tools when enabled server-side.
- Admin: usage rollups and Azure Monitor resource panels, hidden from non-admins
  in the UI and enforced by the API, plus fixed-KQL operations/security panels
  with explicit source, freshness, partial, stale, and unavailable states.

## Local dev

Run from `app/web`, with the [API](../api/README.md#local-dev) on port 8080.

```powershell
npm ci
# First setup only; retain an existing local .env.local.
Copy-Item .env.example .env.local
npm run dev
```

Use the Node 22 range declared by `package.json`'s `engines.node` to match CI
and the production image. npm only warns about engine mismatches; it does not
prevent an unsupported local run. Corporate registry and dependency-junction
workarounds are documented in [AGENTS.md](../../AGENTS.md), not in the lockfile.

Run checks from this folder:

```powershell
npm run lint
npm test
npm run build
```

## Runtime configuration

Server-side env in `src/app/(protected)/layout.tsx` controls auth and feature
visibility; it is read at request time (not frozen at build), so deploy-time
config can change without a rebuild. The root layout stays auth-free so 404 and
error routes remain renderable. Avoid `NEXT_PUBLIC_*` for these gates.

| Var | Purpose |
| --- | --- |
| `WEB_AUTH_PROVIDER` | `dev` or `entra` |
| `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID` / `ENTRA_API_SCOPE` | MSAL SPA settings (Entra mode) |
| `ENTRA_REDIRECT_URI` | Optional MSAL redirect URI; defaults to `window.location.origin` |
| `VOICE_LIVE_ENABLED` + `API_PUBLIC_URL` | Show Voice Live and connect the browser directly to the API ingress (the Next.js proxy cannot proxy WebSockets) |
| `VOICE_LIVE_TOOLS_ENABLED` | Offer governed tools inside a live voice session (mirrors the API's `AI4IA_REALTIME_TOOLS_ENABLED`) |
| `DOCUMENT_LIBRARY_ENABLED` | Show the cross-session document library |
| `CUSTOM_TOOLS_ENABLED` | Show user MCP server management |
| `DEV_USER` | Dev-auth identity the proxy injects as `X-Dev-User` (dev only; dropped when unset) |
| `API_BASE_URL` | Backend base URL for the same-origin proxy (server-side only) |

## Current gaps

- There is no global per-user memory enable/disable preference.
- Exact proxy/APIM/provider stage percentiles depend on telemetry dimensions that
  are not available in every environment; unknown/freshness states are explicit.
