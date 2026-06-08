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
