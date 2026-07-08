# app/web — AI4IA Web

Next.js App Router frontend for chat, agents, workflows, voice, document library,
generated media, MCP server management, and the admin dashboard.

## Responsibilities

- Chat shell: sessions, streaming messages, model picker, per-model parameter
  clamps, system prompt editor, document attachments, and command affordances.
- Auth: runtime-selected dev or Entra/MSAL mode. The API remains the enforcement
  boundary; the web app only acquires and forwards tokens.
- Voice: turn-based STT/TTS plus feature-gated Voice Live over the API WebSocket
  relay with model/voice/settings/tool controls.
- Library/media: cross-session document library, ingest status, sharing,
  annotations, save/forget memory actions, audio/video player, and citation
  deep-links.
- Tools: agent builder, workflow builder, imagery/video output rendering, custom
  MCP server management, and Web IQ tools when enabled server-side.
- Admin: usage rollups and Azure Monitor resource panels, hidden from non-admins
  in the UI and enforced by the API.

## Local dev

```powershell
npm ci
npm run dev
```

Use any Node version in the declared `>=22.0.0 <27` range (CI pins Node 22; the
production Docker image is `node:26-alpine`). `package.json` enforces `engines.node`,
so out-of-range local drift fails early instead of surprising you in GitHub Actions.

Run checks from this folder:

```powershell
npm test
npm run build
```

## Runtime configuration

Server-side env in `app/layout.tsx` controls auth and feature visibility; it is read
at request time (not frozen at build), so deploy-time config can change without a
rebuild. Avoid `NEXT_PUBLIC_*` for these gates.

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

- The library upload UI is document-centric; non-document modalities are supported
  backend-side but not first-class in the picker.
- There is no global memory control or recalled-memory indicator in the chat UI.
