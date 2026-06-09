# app/web — AI4IA Web (Next.js + TypeScript)

Chat UI mirroring the Foundry chat experience plus AI4IA extras.

## Responsibilities (phased)
- **Phase 3:** chat surface with streaming, model picker, parameter controls, system-prompt
  editor, document upload; theming (colors/backgrounds/sizing) and high-contrast a11y modes.
- **Phase 4:** `@`-commands (address agents) and `/`-commands (actions) in the composer.
- **Phase 7:** voice (talk to it), custom imagery/backgrounds via gpt-image-2 / MAI-Image-2.
- **Phase 8:** multi-agent linking + workflow builder UI.

## Stack
- Next.js (App Router) + TypeScript, MSAL for auth, server actions / route handlers calling
  the FastAPI backend. Containerized (`Dockerfile`) and deployed to Azure Container Apps.

## Local dev (to be added in Phase 3)
```
npm install
npm run dev
```

> Scaffolding lands in Phase 3. The model picker is driven by the catalog the API exposes
> from `infra/models.json` (never hard-coded).

## Authentication (Phase 9)

The frontend is auth-mode agnostic and selected at **runtime** by server-side env
(read in `app/layout.tsx`, passed as props to the client `AuthProvider` — never
`NEXT_PUBLIC_*`, so nothing is baked into the build):

- **`dev` (default):** no sign-in. The same-origin proxy injects `X-Dev-User` from
  `DEV_USER` and the app renders directly. This is the unchanged dev/demo flow.
- **`entra`:** MSAL (`@azure/msal-browser` + `@azure/msal-react`) gates the app
  behind Microsoft sign-in. `apiFetch` attaches `Authorization: Bearer <token>` to
  every `/api/*` call; the proxy forwards it to the backend (which validates it via
  its own `entra` auth provider). A user chip with **Sign out** appears in the header.

Enable Entra by setting these on the web container (see `.env.example`):

| Var | Purpose |
| --- | --- |
| `WEB_AUTH_PROVIDER` | `dev` (default) or `entra` |
| `ENTRA_CLIENT_ID` | SPA app registration (client) ID |
| `ENTRA_TENANT_ID` | Tenant ID (authority `login.microsoftonline.com/<tenant>`) |
| `ENTRA_API_SCOPE` | API scope the SPA requests, e.g. `api://<api-app-id>/.default` |
| `ENTRA_REDIRECT_URI` | Optional; defaults to the page origin |

If `WEB_AUTH_PROVIDER=entra` but any required value is missing, the app **fails open
to `dev`** — the API is the real auth boundary, so a misconfigured web simply can't
mint tokens (calls 401) rather than rendering an unrecoverable screen.

### App registration (SPA)

1. Register an app in Entra ID → **Single-page application** platform.
2. Add a **Redirect URI** of the web app origin (e.g. `https://<web-fqdn>/`, plus
   `http://localhost:3000/` for local dev).
3. Under **API permissions**, add a delegated permission for the backend API's
   exposed scope (the one matching `ENTRA_API_SCOPE` / the API's audience) and grant
   consent.
4. Set `ENTRA_CLIENT_ID` to this SPA's Application (client) ID and `ENTRA_TENANT_ID`
   to the directory (tenant) ID.

In Azure, `infra/main.bicep` wires these automatically: set `apiAuthProvider=entra`,
`entraWebClientId=<spa-client-id>`, `entraTenantId`, and (optionally) `entraApiScope`
(defaults to `<entraAudience>/.default`). `infra/modules/web.bicep` only emits the
`ENTRA_*` env when the provider is `entra` and all values are present, and stops
injecting `DEV_USER` once Entra is on.

## Voice Live (Phase 10) — real-time speech-to-speech

A **feature-flagged, default-OFF** live voice mode. When off (the default), no
live-voice control is rendered and nothing about the chat UI changes — the existing
turn-based voice (Phase 7B: record → transcribe, and read-aloud) is untouched.

When enabled, a **Live** button appears in the composer (only if the model catalog
also exposes a `realtime` model). It opens a WebSocket **directly to the API's
external ingress** at `/api/voice/live` — the Next.js HTTP proxy can't proxy
WebSockets — and streams 24 kHz mono PCM16 mic audio up while playing the model's
PCM16 response back, with barge-in (playback stops the instant you start speaking).
The API relay enforces all governance (auth, entitlement gate, usage metering,
and `Origin` validation); the browser never sees the deployment or gateway key.

Auth on that WebSocket rides a **subprotocol** (a browser-direct WS can't set an
`Authorization` header): under Entra the client offers `["ai4ia-bearer", <token>]`
(from `getApiAccessToken()`); under dev it offers `["ai4ia-dev", <DEV_USER>]`,
honored by the API only when dev auth is permitted.

Like the auth config, the flag and API URL are read **server-side** (in
`app/layout.tsx`, surfaced via `VoiceLiveProvider` — never `NEXT_PUBLIC_*`):

| Var | Purpose |
| --- | --- |
| `VOICE_LIVE_ENABLED` | `false` (default) or `true` to surface the Live control |
| `API_PUBLIC_URL` | Public https URL of the API ingress; converted to `wss` for the WS |

Both must be set together; a half-config stays disabled. In Azure,
`infra/main.bicep` wires this with the `voiceLiveEnabled` parameter (default
`false`), passing `api.outputs.apiUrl` as `API_PUBLIC_URL` and enabling the API's
realtime relay (`AI4IA_REALTIME_*`). When enabling in a deployed env you must also
set `realtimeAllowedOrigins` (the relay's Origin allowlist) or the relay fails
closed.

## Document library (Phase 11B-2) — cross-session document understanding

A **feature-flagged, default-OFF** personal document library. When off (the
default), no library control is rendered and nothing about the chat UI changes —
the existing per-session chat attachments (Phase 7C) are untouched.

When enabled, a **Document library** panel appears in the sidebar for uploading,
watching ingest status, and deleting files in your cross-session library. Once a
document reaches `ready`, the assistant can reference it in chat: a summary card is
always available, the most relevant excerpts are retrieved per turn, and a
`fetch_document` tool lets tool-enabled agents read more. Only `ready` documents
ever contribute to chat; files that are still ingesting or failed never surface.
The library API goes through the same-origin Next proxy (no public URL needed,
unlike live voice).

Like the auth config, the flag is read **server-side** (in `app/layout.tsx`,
surfaced via `LibraryProvider` — never `NEXT_PUBLIC_*`):

| Var | Purpose |
| --- | --- |
| `DOCUMENT_LIBRARY_ENABLED` | `false` (default) or `true` to surface the library control |

In Azure, `infra/main.bicep` drives this from the same `documentUnderstanding`
parameter that enables the API's ingest/retrieval path, so the UI flag and the
backend feature turn on together. Default `false`.
