# AGENTS.md

Machine-facing contributor guide for AI4IA. Keep it accurate when CI,
architecture, or governance invariants change. There is no
`.github/copilot-instructions.md` or `CLAUDE.md`; if one is added, make it a short
pointer here.

**Deploying** the app is a different job with different rules — see
[`docs/deploy-with-an-agent.md`](docs/deploy-with-an-agent.md).

## What this repo is

AI4IA is a governed, multi-model, multi-region agentic chat app on Azure Container
Apps. The browser uses the Next.js web app; FastAPI owns auth, sessions, tools,
memory, document/library access, usage, and model routing. HTTP/SSE model traffic
— including Responses function calls and the Anthropic Messages adapter — flows
SimpleL7Proxy → APIM → Foundry; realtime/Voice Live WebSockets stay on the
FastAPI relay → APIM path because SimpleL7Proxy does not support WebSockets.

## Monorepo map

- `app/web` — Next.js/TypeScript UI for chat, agents, workflows, voice,
  documents/media, custom MCP server management, auth, and admin dashboards.
- `app/api` — Python FastAPI backend (`ai4ia_api`) for auth, chat, agent/tool
  execution, sessions, documents, memory, usage, metrics, and gateway calls.
- `infra` — Bicep plus azd parameters and catalogs, including the authoritative
  `infra/models.json` model catalog.
- `proxy` — vendored `microsoft/SimpleL7Proxy` source plus AI4IA Dockerfile/notes.
- `scripts` — catalog generators, validators, provisioning helpers, status
  snapshots, teardown/purge scripts, and azd hooks. `scripts/azure-cli.ps1` is a
  shared dot-sourced safety library, not a standalone entry point.
- `docs` and `site` — architecture, runbooks, user/operator docs, and the GitHub
  Pages portal.
- `foundry` — toolbox, routine, and A2A manifests validated by CI.

## Non-negotiable rules

1. **Gateway-first model traffic.** Compatible HTTP/SSE calls (Chat Completions,
   Responses, agents, embeddings, images, videos, REST speech, and Claude Messages) go
   SimpleL7Proxy → APIM → Foundry. FastAPI translates provider schemas but never
   turns that into direct provider egress. Two explicit **SimpleL7Proxy**
   exceptions, both still pass through separately scoped APIM APIs:
   realtime/Voice Live WebSockets take FastAPI relay → APIM → Foundry because the
   proxy has no WebSocket support, and Responses-API Code Interpreter Files +
   stateful sandbox calls take FastAPI → Code Interpreter APIM → Foundry because
   they are not compatible catalog deployments. Direct calls are reserved for
   non-model control/data planes such as Content Understanding, WebIQ grounding,
   Azure Monitor, Key Vault, Blob, Cosmos, and Azure AI Search.
2. **Catalog-driven models.** Do not hardcode deployment names or model lists.
   `infra/models.json` is the source of truth; generated runtime catalog data must
   match it.
3. **Server-authoritative feature gates.** The web app may hide UI, but the API
   and startup validation must enforce feature posture. Never gate only in React.
4. **Cosmos is canonical.** Sessions, messages, usage, user agents/workflows, MCP
   server records, document manifests, and memory text/vectors are canonical and
   scoped per user. Document chunks, search indexes, and parsed artifacts must be
   rebuildable.
5. **Tools re-check at execution time.** Tool execution must re-validate scopes,
   approvals, target hosts, and SSRF/public-HTTPS rules when a call runs, not only
   when a tool/server is registered.
6. **No secret sprawl.** Do not log credentials, commit secrets, or put user MCP
   secrets in Cosmos; durable MCP secrets belong in Key Vault outside local.
7. **Receipts show execution, never hidden reasoning.** Persist bounded,
   credential-redacted prompts/context, source versions, offered/invoked tools,
   arguments/results, approvals, safety coverage and correlation metadata. Never
   label observable traces as chain-of-thought or invent model-internal decisions.

## CI build / test / lint commands

These are the commands GitHub Actions runs. Keep local checks aligned.

### Web (`app/web`), Node 22

```powershell
npm ci
npm run lint --if-present
npm test
npm run build --if-present
```

Package scripts resolve to `eslint .`, `vitest run`, and `next build`. Local dev
uses `npm run dev`.

`npm ci` prints benign `ERESOLVE overriding peer dependency` warnings for
`eslint-config-next`'s bundled plugins, whose published peer ranges still cap at
`eslint@^9`. Install exits 0 and everything dedupes to the single installed
`eslint`. Do not downgrade eslint or add overrides to silence it.

### Running the web tests behind a corporate npm mirror

`npm ci` fails on a mirrored network, but the suite still runs. The two obvious
paths fail for different reasons:

| Command | Failure |
| --- | --- |
| `npm ci` (mirror) | **404** — the mirror is reachable but does not carry every lockfile-pinned version |
| `npm ci --registry=https://registry.npmjs.org` | **TLS handshake failure** — the public registry is unreachable |

The discriminator is the **absence of the lockfile**, not `install` vs `ci`.
`npm install` honours a lockfile when present and fails identically. With no
lockfile, npm's resolver picks versions the mirror does have.

```powershell
# 1. Resolve in a scratch dir. Copy ONLY package.json — never package-lock.json.
mkdir D:\ai4ia-web-scratch; copy app\web\package.json D:\ai4ia-web-scratch
cd D:\ai4ia-web-scratch; npm install --no-audit --no-fund

# 2. Junction it in (app/web/.gitignore already ignores /node_modules).
cmd /c mklink /J <repo>\app\web\node_modules D:\ai4ia-web-scratch\node_modules

# 3. These two gates now run.
cd <repo>\app\web; npm test; npm run lint
```

Three caveats. The tree is **not lockfile-exact** — that is why it installs at all;
CI remains authoritative for reproducibility. On a workstation running a
Node major above the pinned 22, `ThemeProvider.test.tsx` fails with
`localStorage is not available because --localstorage-file was not provided`.
That is an engine mismatch, not a defect. Do not "fix" those tests.

And **`npm run build` does not work through the junction at all**, on any drive.
Turbopack refuses to resolve a reparse point whose target lies outside the
project tree and fails during entrypoint discovery, before it compiles a single
source file:

```text
Symlink [project]/node_modules is invalid, it points out of the filesystem root
  - Execution of find_package failed
```

Nothing in that message mentions the junction you created, so it reads as a
project defect. It is not: `tsc --noEmit` and `npm test` both pass against the
same tree. To run the build, replace the junction with a real directory
(`robocopy <scratch>\node_modules <repo>\app\web\node_modules /E /MT:16`) and
delete `app\web\.next` afterwards. That matters because `tsc` does **not** cover
everything the build does — a missing `"use client"` directive on an extracted
component type-checks cleanly and fails only at build time.

### API (`app/api`), Python 3.12

```powershell
python -m pip install --upgrade pip
pip install -e ".[dev,foundry]"
```

Catalog drift checks run from the repo root:

```powershell
python scripts/gen-model-catalog.py --check
python scripts/gen-mcp-catalog.py --check
python scripts/gen-voice-provider-catalog.py --check
```

Then, in `app/api`:

```powershell
uv lock --check
ruff check .
pyright
pytest -q
```

Plus the Cosmos migration script tests from the repo root:

```powershell
pytest -q scripts/tests/test_memory_cosmos_migration.py
```

**Any edit to `app/api/pyproject.toml` must be followed by `uv lock` in the same
commit.** `uv.lock` records the declared specifier alongside resolved versions, so
even a change that moves no package desyncs it and fails the `uv lock --check`
gate. Nothing on the install path reads the lock (`Dockerfile` runs
`pip install .`; CI runs `pip install -e ".[dev]"`), which is why it can rot
silently. This is also why `.github/dependabot.yml` uses `package-ecosystem: uv`
rather than `pip` for `/app/api`: the pip ecosystem edits `pyproject.toml` and
cannot see `uv.lock`. `scripts/tests/test_dependabot_config.py` fails if that
pairing regresses.

**Run `uv lock` against public PyPI.** Behind a corporate package mirror, `uv lock`
silently rewrites every artifact URL in the lockfile to the internal proxy. Those
URLs are inert until something actually reads the lock, at which point it breaks
for everyone — and the lock meanwhile lies about provenance and leaks internal
feed identifiers into a public repo. Re-run with
`UV_INDEX_URL=https://pypi.org/simple` or off the proxied network.
`scripts/tests/test_lockfile_provenance.py` fails on a single non-PyPI URL and
refuses to pass vacuously on a truncated lock.

**On that same network, `uv lock --check` also fails on a pristine checkout**, and
its hint tells you to do the harmful thing. `--check` re-resolves against *your*
configured index, so a mirror-resolved result never matches a PyPI-resolved lock.
CI is the authority: it runs the same command from PyPI. Before "fixing" a local
failure, confirm CI actually fails and that you changed `pyproject.toml` at all.

**A third-party module imported inside a function body still has to be declared in
`pyproject.toml`.** The API imports heavy/optional SDKs lazily on purpose, and
neither `pyright` nor `pytest` notices when such a dependency goes missing — the
break first appears as a production `ImportError`.
`app/api/tests/test_lazy_imports_are_declared.py` re-derives the lazy imports from
source on every run.

### Base image pins

`app/web/Dockerfile` and `app/api/Dockerfile` pin their bases as
`FROM node:22-alpine@sha256:...` / `FROM python:3.12-slim@sha256:...` — the tag for
humans and Dependabot, the digest for enforcement. Without the digest, a PR build
and a later `azd deploy` can resolve the same tag to different images with no diff
anywhere. The MAJOR(.MINOR) must track the CI version deliberately;
`scripts/tests/test_base_image_pins.py` enforces that against `app-ci.yml` and
fails if a pin is dropped or the multi-stage web file's stages desync.

Refresh with:

```powershell
docker buildx imagetools inspect node:22-alpine --format '{{json .Manifest.Digest}}'
```

That prints the **manifest list / OCI image index** digest, which is the only
correct value. Two ways to grab a platform-specific digest by accident, neither
catchable by CI (which runs on amd64): `docker inspect` on a locally pulled image
returns the amd64 digest because the daemon holds only its own platform's
manifest; and the `Manifests:` block printed by bare `imagetools inspect` lists one
entry per platform. Either makes the image unbuildable on every other
architecture, failing with a platform mismatch that never mentions the pin.

Do not assume Dependabot refreshes the digest — it can suppress a digest-only
update of an unchanged floating tag. Treat it as a manual audit step.

### Docker image builds

`docker-build` builds (never pushes) the `app/web`, `app/api`, and `proxy` images
on every PR, so a broken base reference, bad digest pin, or install failure fails
CI instead of surfacing at deploy. It is separate from `quality`'s `hadolint` job,
which only lints Dockerfile syntax:

```powershell
docker buildx build --file app/web/Dockerfile --load app/web
docker buildx build --file app/api/Dockerfile --load app/api
docker run --rm <api-image> python -c "import ai4ia_api.main"
docker buildx build --file proxy/Dockerfile --load proxy
```

The proxy's NuGet restore runs in locked mode, and the final image is blocked on
HIGH/CRITICAL findings under the exact-CVE `proxy/.trivyignore` policy. The job
retains an SPDX SBOM and unsigned build metadata; production signing remains open
work.

The `dockerignore-context` job builds throwaway probe images from each
`.dockerignore` plus synthetic root- and nested-depth dotenv files, proving
secrets are excluded recursively while committed `.env.example` files survive.
Docker's `.dockerignore` matching is **not** recursive by default — a pattern needs
an explicit `**/` prefix. All three files use `**/.env*` / `!**/.env.example` for
that reason:

```powershell
python -m unittest scripts.tests.test_dockerignore_context
```

### Deploying by digest, not by rebuild

`deploy.yml` does not let `azd deploy` build the images. It builds each service
once, pushes to the azd-managed ACR, reads back the digest the registry assigned,
then runs one
`azd deploy <service> --from-package <loginserver>/ai4ia/<service>-<env>@sha256:<digest>`
per service. Digests are written to the job summary, so a running revision traces
back to a commit.

Four properties this depends on, read from the azd source rather than assumed:

- With `--from-package` set, azd injects the supplied artifact and never calls its
  packager, so no `docker build` runs.
- The containerapp target skips ACR login/tag/push and forwards the original
  string whenever the reference parses and carries a registry.
- azd has no first-class digest concept; for `<host>.azurecr.io/repo@sha256:<hex>`
  it sets `Registry` from the leading dot-bearing segment. **A registry-less
  reference silently falls back to azd building and pushing it**, which is why the
  workflow asserts the login server contains a dot.
- `--from-package` is rejected with `--all`, so the deploy is three invocations. A
  service added to `azure.yaml` but not `deploy.yml` would silently stop being
  deployed; `scripts/tests/test_immutable_image_promotion.py` fails on that.

Repository names match azd's own `DefaultImageName` (`<project>/<service>-<env>`,
lowercased) so a local `azd deploy` and CI use the same ACR repositories.

Both the image push step and the custom-domain preflight derive the resource group
in bash as `rg-${AI4IA_WORKLOAD:-ai4ia}-${AZURE_ENV_NAME}`. Neither can ask Bicep,
so `test_immutable_image_promotion.py` *derives* the expected string from
`infra/main.bicep` and `infra/main.parameters.json` and asserts every shell copy
matches. Getting this wrong sends every `az` lookup to a nonexistent resource group
— and the preflight then reads a missing app as "nothing bound", failing silently.

`deploy.yml` passes `--expect-image <service>=<reference>` to
`post-deploy-verify.py verify`, asserting the app runs exactly the digest this run
pushed. A digest is content-addressed, so an identical rebuild yields an identical
reference; the older "new revision, changed image string" heuristic remains only as
a fallback for callers that cannot name the image. Do not drop those flags.

Rollback state is captured **before `azd provision`**, not merely before
application deployment: all three Bicep app modules use a quickstart placeholder
image for greenfield creation, so an infrastructure reconciliation can create a
placeholder revision before the image build starts. Capturing afterward would make
that placeholder the rollback target.

```powershell
python -m unittest scripts.tests.test_base_image_pins
python -m unittest scripts.tests.test_immutable_image_promotion
```

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
python scripts/provision-foundry-routine.py --check
python scripts/provision-foundry-a2a.py --check
python scripts/validate-catalog.py
python scripts/gen-gateway-policy.py --check
python scripts/gen-voice-provider-catalog.py --check
python scripts/validate-feature-prereqs.py
python -m unittest scripts.tests.test_gateway_policy
python -m unittest scripts.tests.test_policy_json_shape
python -m unittest scripts.tests.test_voice_provider_catalog
python -m unittest scripts.tests.test_feature_prereqs
python -m unittest scripts.tests.test_model_capacity_profile
python -m unittest scripts.tests.test_foundry_local_auth scripts.tests.test_foundry_role_scope scripts.tests.test_web_auth_config scripts.tests.test_postgres_retired scripts.tests.test_runtime_rbac_and_model_pins
python -m unittest scripts.tests.test_rai_policy
python -m unittest scripts.tests.test_bicep_naming
python -m unittest scripts.tests.test_cosmos_backup_policy
python -m unittest scripts.tests.test_lean_azure_iac
python -m unittest scripts.tests.test_bicep_compilation  # fails on diagnostics; inspects compiled ARM
bicep build infra/main.bicep --stdout > /dev/null
```

`infra-validate` installs a pinned standalone Bicep CLI (`BICEP_VERSION` in the
workflow — never `releases/latest`, for reproducibility). Locally,
`az bicep build --file infra/main.bicep --stdout` is equivalent.

The Foundry toolbox schema and provisioner both require an inline A2A `baseUrl` to
be a public HTTPS endpoint without credentials, query, fragment, loopback,
private, link-local, or reserved IP space. Prefer a `projectConnectionId`, which
keeps endpoint and authentication configuration in the Foundry project connection.

`quality` runs actionlint + shellcheck over workflows, PSScriptAnalyzer on
`scripts`, hadolint on the three Dockerfiles, the proxy .NET build/auth tests,
`python3 -m yamllint -c .yamllint .`, a docs-catalog drift gate
(`python scripts/gen-docs-catalog.py --check`), and these contract tests:

```powershell
python3 -m unittest scripts.tests.test_voice_live_canary        # canary URL/redaction rules
python3 -m unittest scripts.tests.test_subscription_preflight   # provider/model preflight logic
python3 -m unittest scripts.tests.test_postprovision_appconfig_sentinel scripts.tests.test_postprovision_cu_defaults scripts.tests.test_postprovision_hard_gates
python3 -m unittest scripts.tests.test_provision_entra_apps     # Entra app bootstrap
python3 -m unittest scripts.tests.test_custom_domain_preflight  # executes deploy.yml's real block with `az` stubbed
python3 -m unittest scripts.tests.test_pages_status_refresh     # status refresh targets live RG/URLs and fails closed
python3 -m unittest scripts.tests.test_status_snapshot_labels   # live services have portal labels/cards
python3 -m unittest scripts.tests.test_portal_contrast          # WCAG gate for site/assets/styles.css
python3 -m unittest scripts.tests.test_brand_assets             # committed logos: coverage, palette, size
python3 -m unittest scripts.tests.test_dependabot_config
python3 -m unittest scripts.tests.test_lockfile_provenance      # uv.lock must resolve from public PyPI
python3 -m unittest scripts.tests.test_proxy_provenance         # vendored hashes and AI4IA patch list
python3 -m unittest scripts.tests.test_proxy_delivery_contracts # probe suppression and final-image evidence
python3 -m unittest scripts.tests.test_post_deploy_verify       # capture/verify/rollback with Azure stubbed
python3 -m unittest scripts.tests.test_azure_cli_safety         # az exit/subscription assertions, typed purge approvals
python3 -m unittest scripts.tests.test_teardown_data_loss_gate
python3 -m unittest scripts.tests.test_lean_azure_cleanup       # retained-resource migration is exact-ID and never automatic
python3 -m unittest scripts.tests.test_documented_paths_exist   # repo paths named in docs must resolve
python3 -m unittest scripts.tests.test_markdown_anchors         # Markdown #fragment links must resolve
python3 -m unittest scripts.tests.test_markdown_tables          # tables cannot silently swallow rows/columns
python3 -m unittest scripts.tests.test_gating_workflows         # required PR checks always report
python3 -m unittest scripts.tests.test_governance_contracts     # cross-file governance/Foundry/config invariants
python3 -m unittest scripts.tests.test_configuration_reference_reachability  # docs may only name reachable azd vars
python3 -m unittest scripts.tests.test_foundry_assets_workflow  # Foundry handoff stays artifact-scoped
python3 -m unittest scripts.tests.test_dockerignore_context
python3 -m unittest scripts.tests.test_base_image_pins
python3 -m unittest scripts.tests.test_immutable_image_promotion
```

`test_custom_domain_preflight`, `test_pages_status_refresh`,
`test_dependabot_config`, `test_post_deploy_verify`, `test_gating_workflows`,
`test_base_image_pins`, `test_subscription_preflight`,
`test_proxy_delivery_contracts`, and `test_immutable_image_promotion` need
`PyYAML` (pinned in the workflow); `test_immutable_image_promotion` also needs
`bash` and skips without it. The rest are stdlib-only.

The provider preflight derives deployed namespaces from Bicep and also carries the
evidence-backed `Microsoft.ResourceHealth` operational dependency used by the
status snapshot. The snapshot must publish provider/query failure as a source
outage; it must never flatten that failure into zero healthy resources or a
per-resource "no signal" result.

`security-scan` runs Trivy filesystem/config scans and gitleaks over the full
proxy tree. `.trivyignore.yaml` suppresses only the untouched upstream Dockerfile
and Kubernetes sample by exact path, and `.gitleaksignore` suppresses one
historical upstream placeholder by exact fingerprint.
`test_proxy_delivery_contracts` verifies those exceptions never expand onto an
AI4IA-patched vendored file.

The vendored proxy plus AI4IA auth guard tests use .NET 10:

```powershell
dotnet restore proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --locked-mode
dotnet build   proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --configuration Release --no-restore
dotnet test    proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --configuration Release --no-build --no-restore
```

When a proxy project dependency changes, refresh from the top-level test project
with `dotnet restore ... --force-evaluate`. NuGet does not recalculate
`AI4IA.Proxy.Tests/packages.lock.json` when only a referenced project's graph
changes, so Dependabot's direct-project lock updates are incomplete by themselves.
Commit all changed proxy lockfiles. Any change inside the vendored `Shared`,
`Shared-parser`, or `SimpleL7Proxy` scopes must also be declared in
`scripts/gen-proxy-provenance.py`, regenerated against the pinned upstream commit,
and checked with `python scripts/gen-proxy-provenance.py --check`.

### Branch protection on `main`

A ruleset requires a pull request, blocks force-pushes and branch deletion,
requires resolved review threads, and requires a fixed list of status checks.
**Required approving reviews is deliberately 0** — this is a solo-maintained repo,
so requiring an approver would self-block every PR. There are **no bypass actors**,
so the rule applies to admins too.

Only checks emitted on **every** PR can be required: GitHub waits indefinitely for
a required check that is never reported, so requiring a path-filtered workflow
deadlocks every PR that misses its filter. `app-ci`, `infra-validate`, and
`docker-build` therefore run on every pull request. Their `push` triggers keep
their path filters, since a push to `main` does not gate a merge.

Always running is deliberate over a `changes` job gating the real jobs: a bug in
custom change detection would be *worse* than the original gap, reporting success
while skipping the tests. The cost is about four minutes of runner time on a
docs-only PR.

`scripts/tests/test_gating_workflows.py` fails if a `paths:` filter returns under
`pull_request:`, or if a job is renamed out from under the ruleset's context list.
Adding a check is a three-step ordering: make it always-reported, prove it on a PR
that would previously have skipped it, then require it. A required context that is
never reported blocks every PR permanently.

## Test discipline: mutate the guard, or you have not written one

A green test says nothing until you have seen it fail for the reason you wrote it.
**Revert your fix and confirm the test fails**, then restore. Failures this has
caught in tests that were already passing: a fake that rejected writes by a
counter rather than by ETag state, so it stayed green with the precondition
removed; a boundary test sitting exactly on the boundary, so nothing overflowed; a
regex that matched neither side of the comparison it claimed to make; a redundant
condition indistinguishable from a load-bearing one until mutated; and a fixture
that made the assertion unreachable, so the test was true for the wrong reason.

Four rules follow:

1. **Prove non-vacuity in both directions.** "Denied when over limit" proves
   nothing unless the identical call is *allowed* when under it. A canary test must
   also demonstrate the egress it prevents actually happens with the gate off.
2. **Pair every "X did not happen" with a control proving X happens when it
   should**, using the *same fixture* with only the condition under test flipped.
   An absence assertion over a code path that never ran is indistinguishable from a
   working guard.
3. **Commit before mutating.** `git checkout -- <file>` also silently discards
   uncommitted work. Back up the bytes and restore from the backup. PowerShell
   rewrites line endings, which has produced false CRLF mutation failures.
4. **A green suite after a rebase is not coverage of the seam the sibling change
   created.** When two branches touch one file and both merge green, CI has covered
   only the union of the tests that already existed. Mutate the new seam, and pair
   it with a control proving your test actually enters the new path.

## How to add things

### Add a chat tool

- For safe built-ins, add a `ToolDefinition` in
  `app/api/src/ai4ia_api/agents/tool_exec.py` with a `ToolSpec`, JSON schema, and
  handler; register it through `build_tools`.
- If users may attach it to agents, update the explicit allowlist in
  `attachable_tool_names`; safe registration alone is not enough.
- For service-backed or external tools, integrate through the chat/router execution
  seam, declare risk/scopes/egress/approval accurately, redact logs, and re-run
  `ToolRegistry.authorize` plus SSRF host validation at execution time.
- Add API tests for authorization, validation, failure handling, and redaction.

### Add a Foundry skill

- Author instruction-only skills at `foundry/skills/<name>/SKILL.md` using the
  Agent Skills front matter (`name`, `description`) and add an unpinned reference
  to `foundry/toolbox.manifest.json`.
- Run `python scripts/provision-foundry-toolbox.py` for offline source/manifest
  validation. The approved `--create` path reconciles immutable skill versions
  before the toolbox and reuses matching versions after interrupted activation.
- Skills are discovered only from generated official-catalog entries with
  `resourcesEnabled`; never accept BYO MCP resources as instructions.
- Preserve progressive disclosure: advertise bounded name/description metadata,
  load the full resource only through `load_skill`, and retain URI, version/default
  resolution, content digest, and truncation provenance in execution receipts.
- A loaded skill cannot weaken system instructions, scope/ownership checks,
  egress policy, or per-invocation approval. Supplementary scripts/assets and
  user-authored skill CRUD require a separate design and threat review.

### Add a model

1. Edit `infra/models.json` first; include category, format, provider `api`,
   version, regions/SKUs/capacity, and metadata such as context/output limits.
2. Run:

   ```powershell
   python scripts/gen-model-catalog.py
   python scripts/gen-model-catalog.py --check
   python scripts/gen-gateway-policy.py
   python scripts/gen-gateway-policy.py --check
   python scripts/validate-catalog.py
   ```

3. Update docs if the model changes a user-visible capability, provider protocol,
   legal prerequisite, safety posture, or region posture. Never type deployment
   names into app code.
4. A new provider protocol needs a tested adapter in
   `app/api/src/ai4ia_api/gateway`, generated APIM routing/auth changes,
   non-streaming plus SSE tool-call controls, and an end-to-end agent-loop test. A
   catalog row alone is not a working integration.
5. **The model's `category` must be in `ROUTABLE_CATEGORIES` in
   `scripts/gen-gateway-policy.py`.** `provider_path` falls back to `"openai"` for
   any unrecognised `api`, so a category with no served surface silently gets a
   plausible-looking OpenAI route that can only 404. Generation now fails instead
   of inventing a route; adding a category to the allowlist without giving it a real
   provider path just moves the failure later.
6. Anthropic deployments additionally require explicit `modelProviderData` and the
   default-off `AI4IA_CLAUDE_ENABLED` gate. Never infer the legal entity, country,
   or industry from tags; `validate-feature-prereqs.py` must fail before provision
   when Claude is enabled and the attestation is missing or placeholder-shaped.

Model deployment `capacity` is the portable baseline. Optional `maxCapacity` values
are subscription-specific output from `scripts/sync-model-capacity.py`; never
hand-copy portal bars or set every regional deployment to the same global limit.
Bicep uses them only when `AI4IA_MODEL_CAPACITY_PROFILE=maximum`.

### Add a feature flag

- Add a default-off `Settings` field in `app/api/src/ai4ia_api/config.py` and
  fail-closed prerequisite checks in `validate_runtime`.
- Wire Bicep parameters, azd/CI variables, and Container App env values in `infra`.
- Document the flag in `docs/configuration-reference.md` and
  `docs/runbooks/feature-enablement.md`.
- If the web needs visibility, expose server-read env in the Next.js runtime
  config; do not use web visibility as enforcement.

### Add an API router

- Add a router under `app/api/src/ai4ia_api/routers`, include it in `main.py`, and
  require `get_current_user` unless the route is intentionally public
  health/config.
- Scope reads/writes by `AuthenticatedUser.internal_user_id`; preserve Cosmos
  partition and ownership patterns.
- Add client helpers in `app/web/src/lib` that call `apiFetch` so Entra bearer
  tokens and dev proxy behavior stay consistent.
- Cover auth, ownership, disabled-feature, and error cases in tests.

### Add or move a documentation page

- The portal's documentation hub (`site/docs.html`) is generated. Edit
  `site/data/docs.manifest.json`, then run `python scripts/gen-docs-catalog.py` to
  regenerate `site/data/docs.js` (never edit `docs.js` by hand).
- Every tracked `*.md` must be either listed in a manifest section or matched by
  the manifest's `exclude` globs — the generator's completeness gate (and the
  `quality` CI `--check`) fails otherwise. Regenerate and commit `docs.js`
  alongside your Markdown change.
- Judge a doc by post-build value: does it help a human or agent understand, use,
  deploy, govern, or extend the running app? Surface those; exclude build-time
  scaffolding and point-in-time status prose.

### Change the brand palette or a logo

The brand is **orange + blue** (complements) over near-black. There are two
independent palettes and two independent gates:

- **App**: `app/web/src/app/globals.css` — `:root` (light) plus
  `[data-theme="dark"]` and `[data-theme="contrast"]`. Gated by
  `app/web/src/app/globals.contrast.test.ts` under `npm test`.
- **Portal**: `site/assets/styles.css` — `:root` (dark) plus a
  `prefers-color-scheme: light` block. Static site, no build, so its gate is
  `scripts/tests/test_portal_contrast.py` under `quality`.

Four rules the gates encode:

1. **`--accent` (app) and `--brand`/`--brand-2` (portal) are dual-purpose** — TEXT
   on the page background *and* a fill under a foreground token. A vivid orange
   satisfies only the second, which is why light mode uses a deeper value for text
   and keeps the vivid one in a separate decoration-only token.
2. **The foreground must follow the fill, not the theme.** A *user-chosen* accent
   inverts the requirement, so `ThemeProvider.readableForeground` derives it per
   accent. Do not reintroduce a fixed per-theme value and do not hardcode
   `color: "#fff"` on a `var(--accent)` background — white on the high-contrast
   theme's yellow measures 1.07:1. `app/web/src/components/themeTokens.test.ts`
   brace-matches each `style={{ ... }}` object and fails on any literal hex assigned
   to a `color:` property. Use `--danger`/`--success`/`--info`/`--warn`.
3. **Do not rebrand `[data-theme="contrast"]`.** It is an accessibility floor, not a
   brand surface.
4. **Keep `--warn` clear of `--accent`.** In light mode the brand accent *is* an
   orange, so the obvious amber collides with it. `globals.contrast.test.ts` asserts
   at least 15 degrees of circular hue separation from both `--accent` and
   `--danger`.

Logos are generated: `python scripts/gen-brand-assets.py` writes **every** committed
raster from one palette definition. It needs Pillow and a bold sans TTF, so it is
deliberately not in CI — run it and commit the output.
`scripts/tests/test_brand_assets.py` gates those bytes with the stdlib, checking
completeness (assets are discovered via `git ls-files`; anything not owned by the
generator must be listed in `NON_BRAND_RASTERS`), colour (≥40% of saturated pixels
near the brand hue), and shape/weight against the portal's declared `og:image`
dimensions and per-file size ceilings.

## Auth model and `apiFetch` contract

- Production auth is Entra bearer-token validation in the API (`aud`, `iss`,
  tenant, signature, expiry). Internal user ids are derived at the API boundary and
  decoupled from the identity provider.
- Local/dev auth uses `X-Dev-User`; the Next.js same-origin proxy is the authority
  that injects or drops it. Browser-supplied `X-Dev-User` must not be trusted.
- `apiFetch` is the browser helper for same-origin `/api/*` calls. In Entra mode it
  silently acquires an MSAL token and adds only `Authorization`; in dev mode it is a
  pass-through so the server-side proxy controls identity. Keep uploads
  multipart-safe by not forcing `Content-Type`.

## RBAC by hand: `--assignee-object-id` takes the PRINCIPAL id

A user-assigned managed identity has **two** GUIDs, and `az` will silently accept
the wrong one. `az role assignment create --assignee-object-id <clientId>
--assignee-principal-type ServicePrincipal` **succeeds**: the principal-type flag
skips directory validation, so the assignment is created against an object that
grants the identity nothing. `az role assignment delete --assignee <clientId>`, by
contrast, *does* resolve the client id back to the real principal — so a
grant-then-revoke pair written against the two different ids leaves the identity
with neither role and no error.

Two rules:

1. Read the principal id from the resource:
   `az identity list -g <rg> --query "[].{n:name,principalId:principalId}"`.
   `az role assignment list` prints the *clientId* in `principalName` for managed
   identities, which is exactly how the wrong value gets copied.
2. **Verify by scope, not by assignee.** `az role assignment list --assignee <id>`
   resolves the id first, so it can report the roles the identity *should* have
   while the actual row belongs to a different object. `--scope <resource>` shows
   the literal `principalId` on each assignment.

## Red flags: stop and ask a human

- You are about to bypass the approved HTTP/SSE proxy → APIM path, bypass APIM for
  realtime, or introduce a direct deployment name.
- A feature would be enabled only in the UI, or a deployed feature lacks durable
  storage/auth/prerequisites.
- You need new Azure resources, RBAC, production secrets, custom domains, or a
  deploy/provision run.
- You are changing `proxy/SimpleL7Proxy` without refreshing its upstream pin and
  notices.
- You would weaken SSRF, approval, scope, entitlement, admin, or per-user ownership
  checks.
- You need to alter existing user data, Cosmos partition keys, migrations, or
  rebuild derived stores.
