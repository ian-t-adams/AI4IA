# AGENTS.md

Canonical machine-facing contributor guide for AI4IA. Keep this file accurate when CI, architecture, or governance invariants change. No `.github/copilot-instructions.md` or `CLAUDE.md` exists today; if one is added, make it a short pointer here.

## What this repo is

AI4IA is a governed, multi-model, multi-region agentic chat app on Azure Container Apps. The browser uses the Next.js web app; FastAPI owns auth, sessions, tools, memory, document/library access, usage, and model routing. Compatible HTTP/SSE model traffic flows SimpleL7Proxy -> APIM -> Foundry; realtime/Voice Live WebSockets stay on the FastAPI relay -> APIM path because SimpleL7Proxy does not support WebSockets.

## Monorepo map

- `app/web` — Next.js/TypeScript UI for chat, agents, workflows, voice, documents/media, custom MCP server management, auth, and admin dashboards.
- `app/api` — Python FastAPI backend (`ai4ia_api`) for auth, chat, agent/tool execution, sessions, documents, memory, usage, metrics, and gateway calls.
- `infra` — Bicep plus azd parameters and catalogs, including the authoritative `infra/models.json` model catalog.
- `proxy` — vendored `microsoft/SimpleL7Proxy` source plus AI4IA Dockerfile/notes.
- `scripts` — catalog generators, validators, provisioning helpers, status snapshots, teardown/purge scripts, and azd hooks.
- `docs` and `site` — architecture, runbooks, audit findings, user/operator docs, and the GitHub Pages portal.
- `foundry` — toolbox, skills, routine, and A2A manifests validated by CI.

## Non-negotiable rules

1. **Gateway-first model traffic.** Compatible HTTP/SSE calls (chat, agents, embeddings, images, videos, and REST speech) go SimpleL7Proxy -> APIM -> Foundry. Realtime/Voice Live WebSockets are the explicit exception: FastAPI relay -> APIM -> Foundry. Do not call Foundry model deployments directly from app code. Direct calls are reserved for non-OpenAI/native control or data planes such as Content Understanding, Azure Monitor, Key Vault, Blob, Cosmos, and Azure AI Search.
2. **Catalog-driven models.** Do not hardcode deployment names or model lists. `infra/models.json` is the source of truth; generated runtime catalog data must match it.
3. **Server-authoritative feature gates.** The web app may hide UI, but the API and startup validation must enforce feature posture. Never gate only in React/Next.js.
4. **Cosmos is canonical.** Sessions, messages, usage, user agents/workflows, MCP server records, and document manifests are canonical and scoped per user. Derived memory vectors, document chunks, search indexes, and parsed artifacts must be rebuildable.
5. **Tools re-check at execution time.** Tool execution must re-validate scopes, approvals, target hosts, and SSRF/public-HTTPS rules when a call runs, not only when a tool/server is registered.
6. **No secret sprawl.** Do not log credentials, commit secrets, or put user MCP secrets in Cosmos; durable MCP secrets belong in Key Vault outside local.

## CI build / test / lint commands

These are the commands GitHub Actions runs today; keep local checks aligned.

### Web (`app/web`)

CI uses Node 22, then runs in `app/web`:

```powershell
npm ci
npm run lint --if-present
npm test
npm run build --if-present
```

Package scripts currently resolve to `eslint .`, `vitest run`, and `next build`. Local dev uses `npm run dev`. Prefer `npm ci` over `npm install` when validating reproducibility.

### API (`app/api` plus repo-root catalog checks)

CI uses Python 3.12. `app/api/Dockerfile`'s `FROM python:3.12-slim` must track this
same version deliberately (see the comment above that line) — a June 2026
Dependabot major-version bump to `python:3.14-slim` went unreviewed for weeks
(no CI job exercised the built image, and nothing else in the repo asserted the
two stay in sync) before it was traced to azure-cosmos/aiohttp `DeprecationWarning`
noise in production logs. Contrast `app/web`'s Node bump: that one was reviewed and
accepted the same day, with `package.json`'s `engines.node` range widened to
document it. In `app/api` it installs:

```powershell
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Then it runs catalog drift checks from the repo root:

```powershell
python scripts/gen-model-catalog.py --check
python scripts/gen-mcp-catalog.py --check
python scripts/gen-voice-provider-catalog.py --check
```

Then it runs in `app/api`:

```powershell
ruff check .
pyright
pytest -q
```

This repo has `app/api/uv.lock`; if using `uv` locally, sync the dev extra and run the same tools through `uv run`, but treat the workflow commands above as authoritative.

### Docker image builds

`docker-build` actually builds (never pushes) the `app/web` and `app/api` container images on any PR/push touching `app/web/**` or `app/api/**`, so a broken/renamed base image tag or an install failure specific to the pinned Node/Python version fails CI instead of only surfacing at `azd deploy`. It is separate from `quality`'s `hadolint` job, which only lints Dockerfile syntax and never resolves an image or installs anything:

```powershell
docker buildx build --file app/web/Dockerfile --load app/web
docker buildx build --file app/api/Dockerfile --load app/api
docker run --rm <api-image> python -c "import ai4ia_api.main"
```

`proxy/Dockerfile` (vendored SimpleL7Proxy) is intentionally out of scope for `docker-build` to avoid touching the gateway build path; it is still linted by `quality`'s `hadolint` job and its binary is compiled/tested by `quality`'s `proxy-dotnet` job. azd owns the real build-and-push path at deploy time (see `azure.yaml`, `deploy.yml`).

### Infra, manifests, and operational quality

`infra-validate` runs:

```powershell
python -m pip install --quiet check-jsonschema
check-jsonschema --schemafile infra/models.schema.json infra/models.json
check-jsonschema --schemafile infra/mcp-servers.schema.json infra/mcp-servers.json
check-jsonschema --schemafile infra/voice-providers.schema.json infra/voice-providers.json
check-jsonschema --schemafile foundry/toolbox.manifest.schema.json foundry/toolbox.manifest.json
check-jsonschema --schemafile foundry/toolbox.manifest.schema.json foundry/toolbox.manifest.example.json
check-jsonschema --schemafile foundry/routines/routine.schema.json foundry/routines/example.routine.json
check-jsonschema --schemafile foundry/a2a/a2a.schema.json foundry/a2a/example.a2a.json
python scripts/validate-catalog.py
python scripts/gen-gateway-policy.py --check
python -m unittest scripts.tests.test_gateway_policy
python scripts/gen-voice-provider-catalog.py --check
python -m unittest scripts.tests.test_voice_provider_catalog
python scripts/validate-feature-prereqs.py
bicep build infra/main.bicep --stdout > /dev/null
```

`infra-validate` installs a pinned standalone Bicep CLI release (`BICEP_VERSION` env var in the workflow, matching the `ACTIONLINT_VERSION`/`HADOLINT_VERSION` pattern used elsewhere — never `releases/latest`, for reproducibility). Locally, if you don't have the standalone `bicep` CLI but already have Azure CLI, `az bicep build --file infra/main.bicep --stdout` produces equivalent output.

`quality` runs actionlint + shellcheck over workflows, PSScriptAnalyzer on `scripts`, hadolint on `app/api/Dockerfile app/web/Dockerfile proxy/Dockerfile`, the proxy .NET build/auth tests, `python3 -m yamllint -c .yamllint .`, a docs-catalog drift gate (`python scripts/gen-docs-catalog.py --check`) that keeps `site/data/docs.js` in sync with `site/data/docs.manifest.json`, and stdlib-only unit tests for operator scripts not already covered by `app-ci`/`infra-validate` (currently `python3 -m unittest scripts.tests.test_voice_live_canary`, covering `scripts/voice-live-canary.py`'s URL/redaction safety rules). `security-scan` runs Trivy filesystem/config scans and gitleaks.

The vendored proxy plus AI4IA auth guard tests use .NET 10:

```powershell
dotnet build proxy/SimpleL7Proxy/SimpleL7Proxy.csproj --configuration Release
dotnet test proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --configuration Release
```

## How to add things

### Add a chat tool

- For safe built-ins, add a `ToolDefinition` in `app/api/src/ai4ia_api/agents/tool_exec.py` with a `ToolSpec`, JSON schema, and handler; register it through `build_tools`.
- If users may attach it to agents, update the explicit allowlist in `attachable_tool_names`; safe registration alone is not enough.
- For service-backed or external tools, integrate through the chat/router execution seam, declare risk/scopes/egress/approval accurately, redact logs, and re-run `ToolRegistry.authorize` plus SSRF host validation at execution time.
- Add API tests for authorization, validation, failure handling, and redaction.

### Add a model

1. Edit `infra/models.json` only; include category, format, version, regions/SKUs/capacity, and metadata such as context/output limits.
2. Run:

   ```powershell
   python scripts/gen-model-catalog.py
   python scripts/gen-model-catalog.py --check
   python scripts/gen-gateway-policy.py
   python scripts/gen-gateway-policy.py --check
   python scripts/validate-catalog.py
   ```

3. Update docs if the model changes a user-visible capability or region posture. Never type deployment names directly into app code.

### Add a feature flag

- Add a default-off `Settings` field in `app/api/src/ai4ia_api/config.py` and fail-closed prerequisite checks in `validate_runtime`.
- Wire Bicep parameters, azd/CI variables, and Container App env values in `infra`.
- Document the flag in `docs/configuration-reference.md` and `docs/runbooks/feature-enablement.md`.
- If the web needs visibility, expose server-read env in the Next.js runtime config; do not use web visibility as enforcement.

### Add an API router

- Add a router under `app/api/src/ai4ia_api/routers`, include it in `main.py`, and require `get_current_user` unless the route is intentionally public health/config.
- Scope reads/writes by `AuthenticatedUser.internal_user_id`; preserve Cosmos partition and ownership patterns.
- Add client helpers in `app/web/src/lib` that call `apiFetch` so Entra bearer tokens and dev proxy behavior remain consistent.
- Cover auth, ownership, disabled-feature, and error cases in tests.

### Add or move a documentation page

- The portal's documentation hub (`site/docs.html`) is generated, not hand-written. Edit
  `site/data/docs.manifest.json` to add, move, retitle, or re-describe a doc, then run
  `python scripts/gen-docs-catalog.py` to regenerate `site/data/docs.js`.
- Every tracked `*.md` must be either listed in a manifest section or matched by the manifest's
  `exclude` globs — the generator's completeness gate (and the `quality` CI `--check`) fails
  otherwise. Regenerate and commit `docs.js` alongside your Markdown change.
- Judge a doc by post-build value: does it help a human or agent understand, use, deploy,
  govern, or extend the running app? Surface those; exclude build-time scaffolding.

## Auth model and `apiFetch` contract

- Production auth is Entra bearer-token validation in the API (`aud`, `iss`, tenant, signature, expiry). Internal user ids are derived at the API boundary and are decoupled from the identity provider.
- Local/dev auth uses `X-Dev-User`; the Next.js same-origin proxy is the authority that injects or drops it. Browser-supplied `X-Dev-User` must not be trusted.
- `apiFetch` is the browser helper for same-origin `/api/*` calls. In Entra mode it silently acquires an MSAL token and adds only `Authorization`; in dev mode it is a pass-through so the server-side proxy controls identity. Keep uploads multipart-safe by not forcing `Content-Type`.

## Red flags: stop and ask a human

- You are about to bypass the approved HTTP/SSE proxy -> APIM path, bypass APIM for realtime, or introduce a direct deployment name.
- A feature would be enabled only in the UI, or a deployed feature lacks durable storage/auth/prerequisites.
- You need new Azure resources, RBAC, production secrets, custom domains, or a deploy/provision run.
- You are changing `proxy/SimpleL7Proxy` without refreshing its upstream pin and notices.
- You would weaken SSRF, approval, scope, entitlement, admin, or per-user ownership checks.
- You need to alter existing user data, Cosmos partition keys, migrations, or rebuild derived stores.
