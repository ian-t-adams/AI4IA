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

1. **Gateway-first model traffic.** Compatible HTTP/SSE calls (chat, agents, embeddings, images, videos, and REST speech) go SimpleL7Proxy -> APIM -> Foundry. Two explicit exceptions, both documented in `docs/architecture.md`: realtime/Voice Live **WebSockets** take FastAPI relay -> APIM -> Foundry (SimpleL7Proxy has no WebSocket support), and the **Responses-API Code Interpreter** goes direct to Foundry because a stateful Azure-managed sandbox container is not a routable chat-completions deployment. Do not call Foundry model deployments directly from app code outside those two. Direct calls are otherwise reserved for non-OpenAI/native control or data planes such as Content Understanding, Azure Monitor, Key Vault, Blob, Cosmos, and Azure AI Search.
2. **Catalog-driven models.** Do not hardcode deployment names or model lists. `infra/models.json` is the source of truth; generated runtime catalog data must match it.
3. **Server-authoritative feature gates.** The web app may hide UI, but the API and startup validation must enforce feature posture. Never gate only in React/Next.js.
4. **Cosmos is canonical.** Sessions, messages, usage, user agents/workflows, MCP server records, document manifests, and memory text/vectors are canonical and scoped per user. Document chunks, search indexes, and parsed artifacts must be rebuildable.
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

`app/web/Dockerfile`'s `FROM node:22-alpine` must track this same version
deliberately (see the comment above that line). A Dependabot major-version bump
to `node:26-alpine` was merged the same day `package.json`'s `engines.node` was
widened to `>=22.0.0 <27` — but that widening only stopped the manifest
contradicting the image; it was not evidence of an actual Node 26 requirement.
No CI job exercised the built image at that Node version, and every runtime
dependency (`next`, `typescript`, `vitest`) is satisfied by any Node 22.x.
Reverted to `node:22-alpine` on audit; see the parallel Python incident below.

`engines.node` is `>=22.22.2 <23`, not the wider `>=22.0.0`: the floor tracks
whichever direct devDependency demands the most. `eslint@10.6.0` publishes
`engines.node: "^20.19.0 || ^22.13.0 || >=24"`, which set the previous
`>=22.13.0`; `jsdom@30.0.1` then raised it again with
`^22.22.2 || ^24.15.0 || >=26.0.0`, so Node 22.13.0–22.22.1 satisfies the old
floor but not jsdom's actual 22.x requirement. CI's `actions/setup-node@...`
with `node-version: "22"` always resolves to the latest 22.x release (well
above 22.22.2), and `app/web/Dockerfile`'s `node:22-alpine` does the same, so
this has no CI/runtime impact today — it only makes the manifest's stated floor
match what the declared dependencies actually require. There is no `.npmrc`
setting `engine-strict`, so a mismatch would never have failed a build; it would
just have been untrue. Re-check this whenever a devDependency major lands.

jsdom 30 also tightened its CSSOM: it rejects viewport units on
`max-width`/`max-height`, so `toHaveStyle({ maxWidth: "100vw" })` style
assertions no longer see values React did set and real browsers do honour. One
assertion in `Sidebar.test.tsx` relied on that and was narrowed rather than
worked around — jsdom has no layout engine, so those assertions only ever
proved a literal string was passed through. Prefer behavioural assertions over
`toHaveStyle` for anything layout-related.

`npm ci` also prints benign `npm warn ERESOLVE overriding peer dependency`
warnings for `eslint-config-next`'s bundled `eslint-plugin-import` /
`eslint-plugin-jsx-a11y` / `eslint-plugin-react` — their published peer ranges
still cap at `eslint@^9`/`^9.7` as of this writing, and no compatible release
exists yet. Install still exits 0, npm dedupes everything to the single
`eslint@10.6.0` actually installed, and lint/test/build are unaffected. This is
tracked upstream noise, not a local misconfiguration — do not downgrade eslint
or add overrides to silence it.

### API (`app/api` plus repo-root catalog checks)

CI uses Python 3.12. `app/api/Dockerfile`'s `FROM python:3.12-slim` must track this
same version deliberately (see the comment above that line) — a June 2026
Dependabot major-version bump to `python:3.14-slim` went unreviewed for weeks
(no CI job exercised the built image, and nothing else in the repo asserted the
two stay in sync). Production `azure-cosmos`/`aiohttp` warning reports prompted the
review, but the audit did not establish that Python 3.14 caused those warnings.
`app/web`'s Node base drifted the same way (see the Web section above) — both are
now pinned back to the CI-tested majors. In `app/api` it installs:

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
uv lock --check
ruff check .
pyright
pytest -q
```

It also runs the Cosmos migration script tests from the repo root after the API
development dependencies are installed:

```powershell
pytest -q scripts/tests/test_memory_cosmos_migration.py
```

This repo has `app/api/uv.lock`; if using `uv` locally, sync the dev extra and run the same tools through `uv run`, but treat the workflow commands above as authoritative.

**Any edit to `app/api/pyproject.toml` must be followed by `uv lock`**, in the
same commit. `uv.lock` records the *declared specifier* alongside resolved
versions, so even a change that moves no package — raising a ceiling from
`fastapi<0.141` to `<0.142`, say — desyncs it and fails the `uv lock --check`
gate. Nothing on the install path reads the lock (`Dockerfile` runs
`pip install .`, CI runs `pip install -e ".[dev]"`), which is why it silently
rotted four `pypdf` releases behind a live CVE fix before that gate existed.

This is also why `.github/dependabot.yml` uses `package-ecosystem: uv` rather
than `pip` for `/app/api`: the pip ecosystem edits `pyproject.toml` and cannot
see `uv.lock`, so every Python dependency PR it opened failed CI until someone
hand-ran `uv lock`. `scripts/tests/test_dependabot_config.py` fails if that
pairing regresses.

**A third-party module imported inside a function body still has to be declared
in `pyproject.toml`.** The API imports heavy/optional SDKs lazily on purpose, so
the app boots without every Azure service wired — but that also means neither
gate notices when such a dependency goes missing. Measured, not assumed: with
`azure-monitor-query` uninstalled outright, `pyright` reported `0 errors` (an
unresolved submodule of the `azure` namespace package does not fail the type
gate the way a missing top-level module does) and `pytest` stayed green (the
tests inject a fake querier). The break would first appear as a production
`ImportError`. `app/api/tests/test_lazy_imports_are_declared.py` re-derives the
lazy imports from the source on every run and fails if any no longer resolves,
so a newly added one is covered the day it is written.

### Docker image builds

`docker-build` actually builds (never pushes) the `app/web` and `app/api` container images on any PR/push touching `app/web/**` or `app/api/**`, so a broken/renamed base image tag or an install failure specific to the pinned Node/Python version fails CI instead of only surfacing at `azd deploy`. It is separate from `quality`'s `hadolint` job, which only lints Dockerfile syntax and never resolves an image or installs anything:

```powershell
docker buildx build --file app/web/Dockerfile --load app/web
docker buildx build --file app/api/Dockerfile --load app/api
docker run --rm <api-image> python -c "import ai4ia_api.main"
```

`proxy/Dockerfile` (vendored SimpleL7Proxy) is intentionally out of scope for `docker-build` to avoid touching the gateway build path; it is still linted by `quality`'s `hadolint` job and its binary is compiled/tested by `quality`'s `proxy-dotnet` job. azd owns the real build-and-push path at deploy time (see `azure.yaml`, `deploy.yml`).

`docker-build`'s `dockerignore-context` job builds separate, throwaway probe images from `app/web/.dockerignore` and `app/api/.dockerignore` plus synthetic root- and nested-depth dotenv files to prove secrets are excluded from the Docker build context recursively (Docker's `.dockerignore` matching is not recursive by default the way Git's `.gitignore` is — a pattern needs an explicit `**/` prefix to match at every depth) while committed `.env.example` files still survive:

```powershell
python -m unittest scripts.tests.test_dockerignore_context
```

Both `.dockerignore` files use `**/.env*` / `!**/.env.example` for exactly this reason; do not narrow either back to an unanchored `.env*` without re-running this test.

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

`quality` runs actionlint + shellcheck over workflows, PSScriptAnalyzer on `scripts`, hadolint on `app/api/Dockerfile app/web/Dockerfile proxy/Dockerfile`, the proxy .NET build/auth tests, `python3 -m yamllint -c .yamllint .`, a docs-catalog drift gate (`python scripts/gen-docs-catalog.py --check`) that keeps `site/data/docs.js` in sync with `site/data/docs.manifest.json`, and stdlib-only unit tests for operator scripts and CI configuration not already covered by `app-ci`/`infra-validate`:

```powershell
python3 -m unittest scripts.tests.test_voice_live_canary        # voice-live-canary.py URL/redaction rules
python3 -m unittest scripts.tests.test_subscription_preflight   # new-subscription provider/model preflight logic
python3 -m unittest scripts.tests.test_provision_entra_apps     # Entra app bootstrap (runs once, by hand, so CI can't)
python3 -m unittest scripts.tests.test_custom_domain_preflight  # executes deploy.yml's real run: block with `az` stubbed
python3 -m unittest scripts.tests.test_portal_contrast          # WCAG gate for site/assets/styles.css (no build, no other runner)
python3 -m unittest scripts.tests.test_brand_assets             # every committed logo: coverage, palette, size
python3 -m unittest scripts.tests.test_dependabot_config        # keeps dependabot.yml and the uv.lock gate in step
```

`test_custom_domain_preflight` and `test_dependabot_config` need `PyYAML` (pinned in the workflow); the rest are stdlib-only. `security-scan` runs Trivy filesystem/config scans and gitleaks.

The vendored proxy plus AI4IA auth guard tests use .NET 10:

```powershell
dotnet build proxy/SimpleL7Proxy/SimpleL7Proxy.csproj --configuration Release
dotnet test proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --configuration Release
```

### Branch protection on `main`

`main` is protected by a repository ruleset: a pull request is required, force-pushes
and branch deletion are blocked, review threads must be resolved, and 11 status checks
must pass. **Required approving reviews is deliberately 0** — this is a solo-maintained
repo, so requiring an approver would self-block every PR. There are **no bypass actors**,
so the rule applies to admins too; turning it off is a visible settings change rather
than a silent `git push`.

Only checks that are emitted on **every** PR are required — the seven `quality` jobs,
both `codeql` analyses, and `security-scan`'s two blocking jobs. `app-ci`,
`infra-validate`, and `docker-build` are **path-filtered**, so they are deliberately
*not* required: GitHub waits indefinitely for a required check that is never reported,
so requiring one would deadlock every docs-only PR. This was verified rather than
assumed — adding a single unreachable context flipped an otherwise-green PR from
`CLEAN` to `BLOCKED`.

The consequence to know: a PR touching `app/**` or `infra/**` still *runs* those
workflows and still shows a red check, but protection does not block the merge on it.
Closing that gap needs an always-run aggregate job per path-filtered workflow (tracked
in `docs/roadmap.md`). If you add such an aggregate, add its context to the ruleset.

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

### Change the brand palette or a logo

The brand is **orange + blue** (complements) over near-black. There are two
independent palettes and two independent gates; changing one without the other is
the drift this section exists to prevent.

- **App**: `app/web/src/app/globals.css` — one `:root` (light) plus
  `[data-theme="dark"]` and `[data-theme="contrast"]` blocks. Gated by
  `app/web/src/app/globals.contrast.test.ts` under `npm test`.
- **Portal**: `site/assets/styles.css` — `:root` (dark) plus a
  `prefers-color-scheme: light` block. It is a static site with no build, so its
  gate is `scripts/tests/test_portal_contrast.py`, run by `quality`.

Three rules the gates encode, each of which was violated at some point:

1. **`--accent` (app) and `--brand`/`--brand-2` (portal) are dual-purpose** — TEXT
   on the page background *and* a fill under a foreground token. A vivid orange
   satisfies only the second: `#ea580c` is 3.3:1 on white and fails AA as text.
   That is why light mode uses the deeper `#b4400f` and the vivid value lives in
   the separate, decoration-only `--brand`.
2. **The foreground must follow the fill, not the theme.** `--accent-fg` is right
   only for the accent shipped beside it; a *user-chosen* accent inverts the
   requirement. `ThemeProvider.readableForeground` derives it per accent — do not
   reintroduce a fixed per-theme value, and do not hardcode `color: "#fff"` on a
   `var(--accent)` background.
3. **Do not rebrand `[data-theme="contrast"]`.** It is an accessibility floor, not
   a brand surface; its yellow-on-black measures better than any orange.

Logos are generated, not hand-edited: `python scripts/gen-brand-assets.py` writes
**every** committed raster — the app mark, both favicon ladders, the Next.js app
and Apple icons, the portal icon, the Open Graph lettermark, and all of
`assets/branding/` — from one palette definition. It needs Pillow and a bold sans
TTF, so it is deliberately not wired into CI: run it and commit the output.

`scripts/tests/test_brand_assets.py` gates those committed bytes with the stdlib
(no Pillow), and the three things it checks map to three failures that have
actually happened here:

- **Completeness.** The first orange rebrand regenerated four assets and missed
  six, including the web app's own `favicon.ico`. The gate now discovers rasters
  via `git ls-files` and fails on anything not owned by the generator or listed in
  `NON_BRAND_RASTERS`. If you add an image, that is a deliberate choice you must
  record.
- **Colour.** Those six missed files had the right dimensions and the right
  weight; they were simply still azure. Only pixels catch that, so the gate
  decodes them and requires ≥40% of saturated pixels to sit near the brand hue —
  current assets score ~75%, the old azure mark scores 0%.
- **Shape and weight.** Real size vs `site/index.html`'s declared `og:image`
  dimensions, the ~1.91:1 Open Graph ratio, and per-file plus aggregate ceilings.
  Size each asset against how it is actually rendered; the portal icon was once
  1024x1024 (750 KB) behind a 30 px box.

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
