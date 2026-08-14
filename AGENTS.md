# AGENTS.md

Canonical machine-facing contributor guide for AI4IA. Keep this file accurate when CI, architecture, or governance invariants change. No `.github/copilot-instructions.md` or `CLAUDE.md` exists today; if one is added, make it a short pointer here.

## What this repo is

AI4IA is a governed, multi-model, multi-region agentic chat app on Azure Container Apps. The browser uses the Next.js web app; FastAPI owns auth, sessions, tools, memory, document/library access, usage, and model routing. HTTP/SSE model traffic — including the Anthropic Messages adapter — flows SimpleL7Proxy -> APIM -> Foundry; realtime/Voice Live WebSockets stay on the FastAPI relay -> APIM path because SimpleL7Proxy does not support WebSockets.

## Monorepo map

- `app/web` — Next.js/TypeScript UI for chat, agents, workflows, voice, documents/media, custom MCP server management, auth, and admin dashboards.
- `app/api` — Python FastAPI backend (`ai4ia_api`) for auth, chat, agent/tool execution, sessions, documents, memory, usage, metrics, and gateway calls.
- `infra` — Bicep plus azd parameters and catalogs, including the authoritative `infra/models.json` model catalog.
- `proxy` — vendored `microsoft/SimpleL7Proxy` source plus AI4IA Dockerfile/notes.
- `scripts` — catalog generators, validators, provisioning helpers, status snapshots, teardown/purge scripts, and azd hooks.
- `docs` and `site` — architecture, runbooks, audit findings, user/operator docs, and the GitHub Pages portal.
- `foundry` — toolbox, routine, and A2A manifests validated by CI.

## Non-negotiable rules

1. **Gateway-first model traffic.** HTTP/SSE calls (chat, agents, embeddings, images, videos, REST speech, and Claude Messages) go SimpleL7Proxy -> APIM -> Foundry. FastAPI translates provider schemas, but never turns that into direct provider egress. Two explicit exceptions, both documented in `docs/architecture.md`: realtime/Voice Live **WebSockets** take FastAPI relay -> APIM -> Foundry (SimpleL7Proxy has no WebSocket support), and the **Responses-API Code Interpreter** goes direct to Foundry because a stateful Azure-managed sandbox container is not a routable catalog deployment. Do not call Foundry model deployments directly from app code outside those two. Direct calls are otherwise reserved for non-model/native control or data planes such as Content Understanding, Azure Monitor, Key Vault, Blob, Cosmos, and Azure AI Search.
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

### Running the web tests locally behind the corporate mirror

**`npm ci` fails on a maintainer workstation, but the web suite still runs — and
it is worth the five minutes to set up.** Treating CI as the only possible gate
costs a full round-trip to learn about a typo; a TypeScript syntax error reached
`main`'s PR queue that way during the P1-14 work.

Both obvious paths fail, for *different* reasons, which is why this looks
hopeless at first:

| Command | Failure |
| --- | --- |
| `npm ci` (default registry) | **404** — `packagefeedproxy.microsoft.io` is reachable but *incomplete*; it does not carry the lockfile-pinned `vite@8.1.3` |
| `npm ci --registry=https://registry.npmjs.org` | **`ERR_SSL_SSL/TLS_ALERT_HANDSHAKE_FAILURE`** — the public registry is complete but unreachable from this network |

**The discriminator is the absence of the lockfile, not the `install` vs `ci`
verb.** `npm install` does *not* ignore a lockfile — it honours one when present
and fails identically. Controlled experiment, same `package.json`, same command,
same network, only the lockfile's presence varying:

| scratch dir contents | `npm install` result |
| --- | --- |
| `package.json` + `package-lock.json` | **404** on `vite@8.1.3` — byte-identical to the `npm ci` failure |
| `package.json` only | **525 packages in ~3 min**, resolving `vite@8.2.0` |

The mechanism in one line: the lockfile pins `vite@8.1.3`, whose tarball the
mirror lacks; with no lockfile npm's resolver picks `8.2.0`, which the mirror has.

So in step 1 below, copy **only** `package.json` — never `package-lock.json`.
Running `npm install` inside `app/web` fails exactly like `npm ci`, because the
lockfile is already sitting there.

```powershell
# 1. Resolve into a scratch dir so a non-lockfile-exact tree never sits in the repo.
mkdir D:\ai4ia-web-scratch; copy app\web\package.json D:\ai4ia-web-scratch
cd D:\ai4ia-web-scratch; npm install --no-audit --no-fund

# 2. Junction it in (app/web/.gitignore already ignores /node_modules).
cmd /c mklink /J <repo>\app\web\node_modules D:\ai4ia-web-scratch\node_modules

# 3. Both gates now run.
cd <repo>\app\web; npm test; npm run lint
```

Measured result: **602 of 611 vitest tests pass, `npm run lint` exits 0.**

Two honesty caveats, because this is a fast pre-check and **not** a replacement
for `app-ci/web`:

- The tree is **not lockfile-exact** — that is the whole reason it installs at
  all. CI remains authoritative for reproducibility.
- The **9 failures are a local Node artifact, not a defect**: every one is in
  `ThemeProvider.test.tsx`, failing with `localStorage is not available because
  --localstorage-file was not provided`. The cause is an engine mismatch that npm
  prints during the install — `EBADENGINE ... required: { node: '>=22.22.2 <23' },
  current: { node: 'v26.4.0' }`. The project pins Node 22; a workstation on Node
  26 gets a runtime where `localStorage` moved behind `--localstorage-file`. Do
  not "fix" those tests. Pass `--localstorage-file` if you need them green
  locally, and expect other Node-26-only artifacts from the same mismatch.

Node 26 can also run `.ts` directly via `--experimental-strip-types`, which is
enough to mutation-test a pure TypeScript module before the junction exists.

`app/web/Dockerfile` pins its base as `FROM node:22-alpine@sha256:...` — the tag
for humans and Dependabot, the digest for enforcement (audit finding P1-7). A
tag is a mutable pointer, so without the digest `docker-build` on a PR and
`azd deploy` later can resolve `22-alpine` to different images with no diff
anywhere to show it. `docker-build` builds all three service images on every PR, so a
wrong digest fails there rather than at deploy time.

Refresh with
`docker buildx imagetools inspect node:22-alpine --format '{{json .Manifest.Digest}}'`,
which prints the **manifest list / OCI image index** digest — the only correct
value here. (Verified: it returns exactly the pinned digest, and
`--format '{{.Manifest.MediaType}}'` on the same reference returns
`application/vnd.oci.image.index.v1+json`.)

There are two ways to grab a **platform-specific** digest by accident. Both look
identical in shape, and neither can be caught by CI, because `docker-build` runs
on amd64 and an amd64-only pin passes every check:

1. **`docker inspect` on a locally pulled image.** The daemon only holds the
   manifest for its own platform, so `RepoDigests` gives you the amd64 digest
   rather than the index. This is the likelier mistake, since `docker inspect`
   is the more familiar command.
2. **Copying from the `Manifests:` block** that bare
   `docker buildx imagetools inspect node:22-alpine` prints beneath the index
   digest — one entry per platform.

Either way the image becomes unbuildable on every other architecture, and it
fails confusingly: a local `azd deploy` from an arm64 Mac reports a
manifest/platform mismatch that never mentions the pin. Both current pins were
confirmed to be index digests. All three `FROM` lines in the multi-stage web
file must carry the same digest.

Do not assume the digest refreshes itself. dependabot-core parses and rewrites
`tag@sha256:` pairs (`docker/lib/dependabot/docker/file_parser.rb` `FROM_LINE`,
`update_checker.rb` `#updated_requirements`), but it suppresses a *digest-only*
refresh of an unchanged **comparable** tag behind the server-side
`docker_digest_only_update_suppression` experiment, whose state is not
observable from this repo — and `22-alpine` is a floating tag whose name never
changes. Treat the refresh as a manual audit step.
`scripts/tests/test_base_image_pins.py` fails if a pin is dropped, if the stages
desync, or if the pinned major stops tracking `app-ci.yml`.

The MAJOR must track this same CI version deliberately. A Dependabot
major-version bump to `node:26-alpine` was merged the same day `package.json`'s
`engines.node` was widened to `>=22.0.0 <27` — but that widening only stopped
the manifest contradicting the image; it was not evidence of an actual Node 26
requirement. No CI job exercised the built image at that Node version, and every
runtime dependency (`next`, `typescript`, `vitest`) is satisfied by any Node
22.x. Reverted to `node:22-alpine` on audit; see the parallel Python incident
below.

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

CI uses Python 3.12. `app/api/Dockerfile` pins its base as
`FROM python:3.12-slim@sha256:...`, for the same reasons and with the same
refresh recipe as the Node base above — and this is where the moving-tag risk
was measured rather than assumed: `python:3.12-slim` moved to a new digest on
2026-08-05, the day before the pin was taken. The MAJOR.MINOR must track this
CI version deliberately — a June 2026 Dependabot major-version bump to
`python:3.14-slim` went unreviewed for weeks (no CI job exercised the built
image, and nothing else in the repo asserted the two stay in sync). Production
`azure-cosmos`/`aiohttp` warning reports prompted the review, but the audit did
not establish that Python 3.14 caused those warnings. `app/web`'s Node base
drifted the same way (see the Web section above) — both are now pinned back to
the CI-tested majors, and `scripts/tests/test_base_image_pins.py` now enforces
that against `app-ci.yml` instead of leaving it to prose. In `app/api` it
installs:

```powershell
python -m pip install --upgrade pip
pip install -e ".[dev,foundry]"
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

**Run `uv lock` against public PyPI, and check what it rewrote.** On a machine
behind a corporate package mirror — which includes at least one maintainer
workstation — `uv lock` silently rewrites *every* artifact URL in the lockfile to
the internal proxy (`packagefeedproxy.microsoft.io`,
`ms-feed-*.pkgs.visualstudio.com`). This is not theoretical: commit `aad6889`
committed a `uv.lock` with **1,659** such lines, `app-ci` passed green, and the
file was repaired two PRs later only because a Dependabot PR happened to
regenerate it from PyPI.

None of the existing gates catch it. `uv lock --check` only compares the lock
against `pyproject.toml`; it never inspects the registry. And because nothing on
the install path reads the lock at all, the poisoned URLs are inert — right up
until something does read it (`uv sync`, a vendoring step, an SBOM generator),
at which point it breaks for every contributor and every CI runner, against a
host most readers will not recognise. It also makes the lock *lie about
provenance*, with hashes alongside lending the wrong answer false authority, and
leaks internal feed GUIDs into a public repo.

If your `uv lock` output diffs thousands of lines you did not intend, this is
why. Re-run it with `UV_INDEX_URL=https://pypi.org/simple`, or off the proxied
network. `scripts/tests/test_lockfile_provenance.py` (run by `quality`) fails on
a single non-PyPI URL, and refuses to pass vacuously on a truncated lock.

**On that network, `uv lock --check` also fails on a pristine checkout — and its
own hint tells you to do the harmful thing.** This is the trap that makes the
above self-reinforcing, so recognise it rather than acting on it. Measured on
`origin/main` at `823d638`, working tree clean, lockfile provably fine (the
provenance test passes: zero non-PyPI URLs):

```
$ uv lock --check
error: The lockfile at `uv.lock` needs to be updated, but `--check` was provided.
hint: To update the lockfile, run `uv lock`.
```

Nothing is wrong. `--check` re-resolves against *your* configured index, and a
mirror-resolved result never matches a PyPI-resolved lock, so it reports drift
that does not exist. Follow the hint and you commit ~1,659 rewritten URLs — which
is exactly how the original incident happened. **CI is the authority here:**
`app-ci`'s `api` job runs the same command from PyPI and passes. Before
"fixing" a local `uv lock --check` failure, confirm CI actually fails it, and
confirm you changed `pyproject.toml` at all — if you did not, there is nothing to
re-lock.

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

`docker-build` actually builds (never pushes) the `app/web`, `app/api`, and `proxy` container images on every PR (and on relevant pushes to `main`), so a broken base reference, bad digest pin, or install failure fails CI instead of only surfacing at `azd deploy`. It is separate from `quality`'s `hadolint` job, which only lints Dockerfile syntax and never resolves an image or installs anything:

```powershell
docker buildx build --file app/web/Dockerfile --load app/web
docker buildx build --file app/api/Dockerfile --load app/api
docker run --rm <api-image> python -c "import ai4ia_api.main"
docker buildx build --file proxy/Dockerfile --load proxy
```

Because every service image is built here on every PR, their base-image digest pins are trustworthy: a digest that does not resolve fails on the PR that introduces it. The proxy's .NET SDK and ASP.NET chiseled runtime both use manifest-list digests, its NuGet restore runs in locked mode, and the final loaded proxy image is blocked on HIGH/CRITICAL findings under the exact-CVE `proxy/.trivyignore` policy. The job retains an SPDX SBOM and unsigned build metadata; production signing/verification remains a roadmap gap until an approved identity/key design exists.

`docker-build`'s `dockerignore-context` job builds separate, throwaway probe images from `app/web/.dockerignore`, `app/api/.dockerignore`, and `proxy/.dockerignore` plus synthetic root- and nested-depth dotenv files to prove secrets are excluded from every Docker build context recursively (Docker's `.dockerignore` matching is not recursive by default the way Git's `.gitignore` is — a pattern needs an explicit `**/` prefix to match at every depth) while committed `.env.example` files still survive:

```powershell
python -m unittest scripts.tests.test_dockerignore_context
```

All three `.dockerignore` files use `**/.env*` / `!**/.env.example` for exactly this reason; do not narrow one back to an unanchored `.env*` without re-running this test.

### Deploying by digest, not by rebuild

`deploy.yml` does **not** let `azd deploy` build the images. It builds each of the three services once (tagged `<commit sha>`), pushes to the azd-managed ACR, reads back the digest the registry assigned, and then runs one `azd deploy <service> --from-package <loginserver>/ai4ia/<service>-<env>@sha256:<digest>` per service. The digests are written to the job summary, so a running revision traces back to a commit.

Four properties this depends on, all read from the azd source at tag `azure-dev-cli_1.29.0` (the version `Azure/setup-azd` installs here) rather than assumed:

- With `--from-package` set, azd injects the supplied artifact and never calls its packager, so no `docker build` runs (`internal/cmd/service_graph.go`).
- The containerapp target skips ACR login/tag/push and forwards `Location: packagePath` — the original string — whenever the reference parses and carries a registry (`pkg/project/service_target_containerapp.go`).
- `ParseContainerImage` has no first-class digest concept, but for `<host>.azurecr.io/repo@sha256:<hex>` it returns no error and sets `Registry` from the leading dot-bearing segment, which is the only field that shortcut reads (`pkg/tools/docker/container_image.go`). **A registry-less reference silently falls back to azd building and pushing it**, which is why the workflow asserts the login server contains a dot.
- `--from-package` is rejected with `--all` and requires a named service, so the deploy is three invocations. A service added to `azure.yaml` and not to `deploy.yml` would silently stop being deployed; `scripts/tests/test_immutable_image_promotion.py` fails on that.

Repository names deliberately match azd's own `DefaultImageName` (`<project>/<service>-<env>`, lowercased) so a local `azd deploy` and CI keep using the same ACR repositories.

Both the image build/push step and the custom-domain preflight derive the resource group in bash as `rg-${AI4IA_WORKLOAD:-ai4ia}-${AZURE_ENV_NAME}`. Neither can ask Bicep, so both are hand-copies — and a copy that merely agrees with the other copy proves nothing. `scripts/tests/test_immutable_image_promotion.py` therefore *derives* the expected string from `infra/main.bicep`'s `var resourceGroupName` and `infra/main.parameters.json`'s `${AI4IA_WORKLOAD=ai4ia}` / `${AZURE_ENV_NAME}` substitutions and asserts every shell copy matches. Getting this wrong sends every `az` lookup in the deploy job to a resource group that does not exist, and the preflight reads a missing app as "nothing bound" — so it fails silently.

This interacts with the P1-6 verification harness, and getting it wrong would roll back healthy releases. `rollout_problems` used to treat "new revision, unchanged image string" as a failure — sound only while azd tagged every build `azd-deploy-<unix-ts>`. A digest is content-addressed, so an identical rebuild yields an identical reference. `deploy.yml` therefore passes `--expect-image <service>=<reference>` to `post-deploy-verify.py verify`, which asserts the app runs *exactly* the digest this run pushed. That is strictly stronger than the old "it changed" heuristic, which remains as the fallback for callers that cannot name the image. Do not drop those flags.

The rollback capture runs **before `azd provision`**, not merely before application
deployment. All three Bicep app modules require an image and use a quickstart
placeholder for greenfield creation, so an infrastructure reconciliation can
create a placeholder revision before image build starts. Capturing afterward
would make that placeholder the rollback target. The rollback gate distinguishes
a manual run that skipped provision (a pre-deploy build/preflight failure touched
nothing) from a run where provision started (restore on provision/build/preflight
failure), while retaining the deliberate no-rollback exception when the
*post-deploy* canary token cannot be reacquired.

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
python -m unittest scripts.tests.test_gateway_policy
python -m unittest scripts.tests.test_policy_json_shape
python scripts/gen-voice-provider-catalog.py --check
python -m unittest scripts.tests.test_voice_provider_catalog
python scripts/validate-feature-prereqs.py
python -m unittest scripts.tests.test_feature_prereqs
python -m unittest scripts.tests.test_model_capacity_profile
python -m unittest scripts.tests.test_foundry_local_auth scripts.tests.test_foundry_role_scope scripts.tests.test_web_auth_config scripts.tests.test_postgres_retired scripts.tests.test_runtime_rbac_and_model_pins
python -m unittest scripts.tests.test_rai_policy
python -m unittest scripts.tests.test_bicep_naming
python -m unittest scripts.tests.test_cosmos_backup_policy
python -m unittest scripts.tests.test_lean_azure_iac
python -m unittest scripts.tests.test_bicep_compilation  # fails on diagnostics; inspects compiled ARM behavior
bicep build infra/main.bicep --stdout > /dev/null
```

`infra-validate` installs a pinned standalone Bicep CLI release (`BICEP_VERSION` env var in the workflow, matching the `ACTIONLINT_VERSION`/`HADOLINT_VERSION` pattern used elsewhere — never `releases/latest`, for reproducibility). Locally, if you don't have the standalone `bicep` CLI but already have Azure CLI, `az bicep build --file infra/main.bicep --stdout` produces equivalent output.

The Foundry toolbox schema and provisioner both require an inline A2A
`baseUrl` to be a public HTTPS endpoint without credentials, query, fragment,
loopback, private, link-local, or reserved IP space. Microsoft documents the
same public-reachability and valid-TLS requirement. Prefer a
`projectConnectionId`, which keeps endpoint and authentication configuration in
the Foundry project connection.

`quality` runs actionlint + shellcheck over workflows, PSScriptAnalyzer on `scripts`, hadolint on `app/api/Dockerfile app/web/Dockerfile proxy/Dockerfile`, the proxy .NET build/auth tests, `python3 -m yamllint -c .yamllint .`, a docs-catalog drift gate (`python scripts/gen-docs-catalog.py --check`) that keeps `site/data/docs.js` in sync with `site/data/docs.manifest.json`, and operator/CI contract tests not already covered by `app-ci`/`infra-validate`:

```powershell
python3 -m unittest scripts.tests.test_voice_live_canary        # voice-live-canary.py URL/redaction rules
python3 -m unittest scripts.tests.test_subscription_preflight   # new-subscription provider/model preflight logic
python3 -m unittest scripts.tests.test_postprovision_appconfig_sentinel scripts.tests.test_postprovision_cu_defaults scripts.tests.test_postprovision_hard_gates
python3 -m unittest scripts.tests.test_provision_entra_apps     # Entra app bootstrap (runs once, by hand, so CI can't)
python3 -m unittest scripts.tests.test_custom_domain_preflight  # executes deploy.yml's real run: block with `az` stubbed
python3 -m unittest scripts.tests.test_pages_status_refresh     # Pages status refresh targets live RG/URLs and fails closed
python3 -m unittest scripts.tests.test_status_snapshot_labels   # live services have human portal labels/cards
python3 -m unittest scripts.tests.test_portal_contrast          # WCAG gate for site/assets/styles.css (no build, no other runner)
python3 -m unittest scripts.tests.test_brand_assets             # every committed logo: coverage, palette, size
python3 -m unittest scripts.tests.test_dependabot_config        # keeps dependabot.yml and the uv.lock gate in step
python3 -m unittest scripts.tests.test_lockfile_provenance      # uv.lock must resolve from public PyPI, not a corporate mirror
python3 -m unittest scripts.tests.test_proxy_provenance         # vendored hashes and explicit AI4IA patch list cannot drift
python3 -m unittest scripts.tests.test_proxy_delivery_contracts # probe suppression and final-image evidence stay wired
python3 -m unittest scripts.tests.test_status_consistency       # roadmap/audit current-state dispositions cannot conflict
python3 -m unittest scripts.tests.test_post_deploy_verify       # executes capture/verify/rollback behavior with Azure stubbed
python3 -m unittest scripts.tests.test_azure_cli_safety         # az exit/subscription assertions and typed purge approvals
python3 -m unittest scripts.tests.test_teardown_data_loss_gate  # destructive teardown requires explicit data-loss acknowledgement
python3 -m unittest scripts.tests.test_lean_azure_cleanup       # retained-resource migration is exact-ID, dry-run, and never automatic
python3 -m unittest scripts.tests.test_documented_paths_exist   # machine-readable source paths named by docs must resolve
python3 -m unittest scripts.tests.test_markdown_anchors         # Markdown #fragment links must resolve to a real heading
python3 -m unittest scripts.tests.test_gating_workflows         # required PR checks always report and match the ruleset inventory
python3 -m unittest scripts.tests.test_markdown_tables          # Markdown tables cannot silently swallow rows/columns
python3 -m unittest scripts.tests.test_documentation_truth      # governance/Foundry/portal/operator claims stay semantically aligned
python3 -m unittest scripts.tests.test_base_image_pins          # base images CI builds must be digest-pinned
python3 -m unittest scripts.tests.test_immutable_image_promotion # legacy filename; content-addressed exact-digest deployment
python3 -m unittest scripts.tests.test_configuration_reference_reachability  # docs may only name azd vars a deploy can actually read
python3 -m unittest scripts.tests.test_foundry_assets_workflow # Foundry handoff remains artifact-scoped and commit-bound
```

`test_custom_domain_preflight`, `test_pages_status_refresh`,
`test_dependabot_config`, `test_post_deploy_verify`,
`test_gating_workflows`, `test_base_image_pins`,
`test_subscription_preflight`,
`test_proxy_delivery_contracts`, and
`test_immutable_image_promotion` needs `PyYAML` (pinned in the workflow).
`test_immutable_image_promotion` additionally needs `bash`
and skips without it.
The rest are stdlib-only. `security-scan` runs Trivy filesystem/config scans and
gitleaks. It scans the full proxy tree: `.trivyignore.yaml` suppresses only the
untouched upstream Dockerfile and Kubernetes sample by exact path, and
`.gitleaksignore` suppresses one historical upstream `key1` placeholder by exact
commit/path/rule/line fingerprint. `test_proxy_delivery_contracts` verifies
those exceptions never expand onto an AI4IA-patched vendored file.

The vendored proxy plus AI4IA auth guard tests use .NET 10:

```powershell
dotnet restore proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --locked-mode
dotnet build proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --configuration Release --no-restore
dotnet test proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --configuration Release --no-build --no-restore
```

When a proxy project dependency changes, refresh from the top-level test project:

```powershell
dotnet restore proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj --force-evaluate
```

NuGet does not recalculate `AI4IA.Proxy.Tests/packages.lock.json` when only a
referenced project's graph changes, so Dependabot's direct-project lock updates
are incomplete by themselves. Commit all changed proxy lockfiles. Any change
inside the vendored `Shared`, `Shared-parser`, or `SimpleL7Proxy` scopes must also
be declared in `scripts/gen-proxy-provenance.py`, regenerated against the exact
pinned upstream commit, and checked with
`python scripts/gen-proxy-provenance.py --check`.

### Branch protection on `main`

`main` is protected by a repository ruleset: a pull request is required, force-pushes
and branch deletion are blocked, review threads must be resolved, and 17 status checks
must pass. **Required approving reviews is deliberately 0** — this is a solo-maintained
repo, so requiring an approver would self-block every PR. There are **no bypass actors**,
so the rule applies to admins too; turning it off is a visible settings change rather
than a silent `git push`.

Only checks that are emitted on **every** PR can be required. GitHub waits indefinitely
for a required check that is never reported, so requiring a path-filtered workflow would
deadlock every PR that misses its filter. This was verified rather than assumed — adding
a single unreachable context flipped an otherwise-green PR from `CLEAN` to `BLOCKED`.

`app-ci`, `infra-validate` and `docker-build` used to be path-filtered and were
therefore excluded, which meant a PR could break the API suite, the Bicep build or the
container images and still be mergeable. They now run on **every** pull request, so
their contexts are always reported and can be required. Their `push` triggers keep their
path filters — a push to `main` does not gate a merge, so there is nothing to deadlock.

Always running is deliberate over the tempting alternative of keeping the filter and
adding a `changes` job to gate the real jobs: a bug in custom change-detection would be
*worse* than the original gap, because it would report success while skipping the tests
entirely. Measured cost of always running is about four minutes of runner time on a
docs-only PR (app-ci ~122s, docker-build ~66s, infra-validate ~40s).

`scripts/tests/test_gating_workflows.py` fails if a `paths:` filter returns under
`pull_request:`, or if a job is renamed out from under the ruleset's context list. If
you add or rename a job in one of those three workflows, update that inventory **and**
the ruleset together — they are two halves of one contract.

The six added contexts are `web`, `api`, `bicep-lint-build`, `web image`, `api image`,
and `dockerignore context boundary`. They were verified to report on a PR touching none
of the old filter paths *before* being required, because a required context that is
never reported blocks every PR permanently. Keep that ordering for any future addition:
make it always-reported, prove it on a PR that would previously have skipped it, then
require it. One consequence to accept knowingly — a docs-only PR now also has to build
both container images, so a flaky Docker build blocks a docs merge.

## Test discipline: mutate the guard, or you have not written one

A green test says nothing until you have seen it fail for the reason you wrote
it. **Revert your fix and confirm the test fails**, then restore. This is not
ceremony here — it has caught four live defects in tests that were already
passing, and each would have shipped a green suite over the bug it was meant to
prevent:

- **A fake that could not model the bug.** A Cosmos double rejected writes by a
  `conflicts` counter rather than by ETag state, so it stayed green with the
  precondition removed — the test most responsible for catching a lost-update
  race could not catch it. Rewritten to be driven purely by ETag state, the way
  real Cosmos behaves (#272).
- **A boundary test sitting exactly on the boundary.** It used precisely 12 keys
  against the old cap of 12, so nothing ever overflowed. Widened to 22 and a
  500-key case (#272).
- **A pattern that matched neither side.** A doc-consistency regex required the
  literal `**closed**`, while the two documents write `**Half closed 2026-08-06.**`
  and `**Partially fixed** — …`. It compared nothing and passed with the defect
  in place (#303).
- **A dead condition that looked defensive.** `isinstance(call_id, str) and
  call_id and not slot.id` — the middle clause is unreachable, because `not
  slot.id` already makes assigning `""` a no-op. Mutating it changed no
  behaviour. **A redundant condition and a load-bearing one are
  indistinguishable until mutated** (#307).
- **A fixture that made the assertion unreachable.** The hardest to spot,
  because the test reads as correct: it asserted a denied account performs no
  upload, and it passed — but its fixture seeded only the *parsed* artifact, so
  the read of the original bytes errored and the upload path was never entered
  at all. It was **true for the wrong reason**, and would have shipped green over
  a gate that ran too late to stop real provider IO (#308).

Three rules that follow from those:

1. **Prove non-vacuity in both directions.** "Denied when over limit" proves
   nothing unless the identical call is *allowed* when under it. A canary test
   must also demonstrate the egress it prevents actually happens with the gate
   off.
2. **Pair every "X did not happen" with a control proving X happens when it
   should** — using the *same fixture*, with only the condition under test
   flipped. That is what caught the fixture bug above: `..._is_gated_before_any_provider_io`
   is only meaningful beside `..._really_does_reach_the_upload`. An absence
   assertion over a code path that never ran is indistinguishable from a working
   guard.
3. **Commit before mutating.** `git checkout -- <file>` to undo a mutation also
   silently discards uncommitted real work. Back up the bytes and restore from
   the backup — and note that PowerShell rewrites line endings, which has
   reported CRLF files as false mutation failures.

**When two branches touch one file and both merge green, CI has only covered the
union of the tests that already existed.** It says nothing about paths the *other*
change created. This is not a rebase-hygiene point — the merge can be textually
clean, the suite can pass, and the coverage hole is still there.

Measured on 2026-08-07, when three sessions ran in parallel:

- #307 (token-streaming the tool loop) merged at 14:33 and added new assistant
  persist paths. #309 (citation provenance) merged **green** at 14:52 on top of
  it. Mutation testing then found that **four of six** persist sites had no test
  holding them: `sources=library_sources` could have been deleted from an
  `@mention` agent turn or a web-search turn with the whole suite passing. Those
  rows persist `sources: null`, which the renderer correctly reads as
  *unattested* — so citations silently revert to pre-P1-14 rendering with nothing
  telling the reader that verification stopped. A silently absent verdict is the
  worst shape this defect takes. Closed by #312, 25 minutes later.
- Separately, two sessions each rewrote the same audit paragraph to say their own
  finding was "honestly partial". Both sentences were true when written and
  **both were false once the other landed**; taking either side of the conflict
  verbatim would have shipped a wrong claim.

So after rebasing onto a sibling's work, do not treat a green suite as coverage.
Mutate the seam the other change introduced, and pair it with a control proving
your test actually enters the new path — #312 asserts `gateway.iterations >= 2`
so a turn that never entered the streaming loop fails instead of quietly
re-testing the old one.

## How to add things

### Add a chat tool

- For safe built-ins, add a `ToolDefinition` in `app/api/src/ai4ia_api/agents/tool_exec.py` with a `ToolSpec`, JSON schema, and handler; register it through `build_tools`.
- If users may attach it to agents, update the explicit allowlist in `attachable_tool_names`; safe registration alone is not enough.
- For service-backed or external tools, integrate through the chat/router execution seam, declare risk/scopes/egress/approval accurately, redact logs, and re-run `ToolRegistry.authorize` plus SSRF host validation at execution time.
- Add API tests for authorization, validation, failure handling, and redaction.

### Add a model

1. Edit `infra/models.json` first; include category, format, provider `api`, version, regions/SKUs/capacity, and metadata such as context/output limits.
2. Run:

   ```powershell
   python scripts/gen-model-catalog.py
   python scripts/gen-model-catalog.py --check
   python scripts/gen-gateway-policy.py
   python scripts/gen-gateway-policy.py --check
   python scripts/validate-catalog.py
   ```

3. Update docs if the model changes a user-visible capability, provider protocol, legal prerequisite, safety posture, or region posture. Never type deployment names directly into app code.
4. A new provider protocol needs a tested adapter in `app/api/src/ai4ia_api/gateway`, generated APIM routing/auth changes, non-streaming plus SSE tool-call controls, and an end-to-end agent-loop test. A catalog row alone is not a working integration.
5. **The model's `category` must be in `ROUTABLE_CATEGORIES` in `scripts/gen-gateway-policy.py`.** `provider_path` falls back to `"openai"` for any unrecognised `api`, so a category with no served surface silently gets a plausible-looking OpenAI route that can only 404. `Cohere-rerank-v4.0-pro` shipped that way — deployed in two regions, emitted into the APIM catalog, billed, and unreachable, because rerank is served only by Cohere's own rerank API. Generation now fails instead of inventing a route; adding a category to the allowlist without giving it a real provider path just moves the failure later. See `docs/region-capability-matrix.md` for the removal and the re-add procedure.
6. Anthropic deployments additionally require explicit `modelProviderData` and the default-off `AI4IA_CLAUDE_ENABLED` gate. Never infer the legal entity, country, or industry from tags; `validate-feature-prereqs.py` must fail before provision when Claude is enabled and the attestation is missing or placeholder-shaped. With the gate off, Bicep and the API catalog must both omit Claude.

Model deployment `capacity` is the portable baseline. Optional `maxCapacity`
values are subscription-specific output from `scripts/sync-model-capacity.py`;
never hand-copy portal bars or set every regional deployment to the same global
limit. `GlobalStandard` pools can be global or regional by model,
`DataZoneStandard` pools are zone-scoped, and the generator infers the scope from
quota plus `modelCapacities` before writing. Bicep uses them only when
`AI4IA_MODEL_CAPACITY_PROFILE=maximum`.

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

Four rules the gates encode, each of which was violated at some point:

1. **`--accent` (app) and `--brand`/`--brand-2` (portal) are dual-purpose** — TEXT
   on the page background *and* a fill under a foreground token. A vivid orange
   satisfies only the second: `#ea580c` is 3.3:1 on white and fails AA as text.
   That is why light mode uses the deeper `#b4400f` and the vivid value lives in
   the separate, decoration-only `--brand`.
2. **The foreground must follow the fill, not the theme.** `--accent-fg` is right
   only for the accent shipped beside it; a *user-chosen* accent inverts the
   requirement. `ThemeProvider.readableForeground` derives it per accent — do not
   reintroduce a fixed per-theme value, and do not hardcode `color: "#fff"` on a
   `var(--accent)` background. This was prose here and was violated anyway, in
   three panels' primary action button: white on the high-contrast theme's yellow
   accent measures **1.07:1**, and 2.26:1 in dark. It is now enforced by
   `app/web/src/components/themeTokens.test.ts`, which brace-matches each
   `style={{ ... }}` object (a fixed-width window spans sibling JSX elements and
   reports false positives) and fails on any literal hex assigned to a `color:`
   property. The same gate covers status colors: `#b91c1c` for an error message
   is 2.67:1 on the dark surface, below even the large-text floor, because the
   literal was chosen against light mode. Use `--danger`/`--success`/`--info`/`--warn`.
3. **Do not rebrand `[data-theme="contrast"]`.** It is an accessibility floor, not
   a brand surface; its yellow-on-black measures better than any orange.
4. **Keep `--warn` clear of `--accent`.** In light mode the brand accent *is* an
   orange, so the obvious amber collides with it — `#92400e` sat 5 degrees away in
   hue and read as brand chrome. `globals.contrast.test.ts` asserts at least 15
   degrees of separation from both `--accent` and `--danger`, using circular hue
   distance so a red-violet cannot pass by wrapping past 360.

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

## RBAC by hand: `--assignee-object-id` takes the PRINCIPAL id

A user-assigned managed identity has **two** GUIDs and `az` will silently accept
the wrong one:

```
id-api-slurmfactory  clientId=39f0bdd7-...  principalId=cd0321eb-...
```

`az role assignment create --assignee-object-id <clientId>
--assignee-principal-type ServicePrincipal` **succeeds**. The `--assignee-principal-type`
flag skips directory validation, so the assignment is created against an object
that grants the identity nothing. `az role assignment delete --assignee <clientId>`,
by contrast, *does* resolve the client id back to the real principal and deletes
the right row.

That asymmetry is a live trap: on 2026-08-07 a role-narrowing change granted the
replacement role to the clientId and deleted the old role from the principalId,
which left Content Understanding with **no** Foundry grant at all until it was
caught. Nothing errored.

Two rules:

1. Read the principal id from the resource, never from a role listing:
   `az identity list -g <rg> --query "[].{n:name,principalId:principalId}"`.
   `az role assignment list` prints the *clientId* in `principalName` for managed
   identities, which is exactly how the wrong value gets copied.
2. **Verify by scope, not by assignee.** `az role assignment list --assignee <id>`
   resolves the id first, so it can report the roles the identity *should* have
   while the actual row belongs to a different object. `--scope <resource>` shows
   the literal `principalId` on each assignment and is the only view that would
   have exposed the mistake.

## Red flags: stop and ask a human

- You are about to bypass the approved HTTP/SSE proxy -> APIM path, bypass APIM for realtime, or introduce a direct deployment name.
- A feature would be enabled only in the UI, or a deployed feature lacks durable storage/auth/prerequisites.
- You need new Azure resources, RBAC, production secrets, custom domains, or a deploy/provision run.
- You are changing `proxy/SimpleL7Proxy` without refreshing its upstream pin and notices.
- You would weaken SSRF, approval, scope, entitlement, admin, or per-user ownership checks.
- You need to alter existing user data, Cosmos partition keys, migrations, or rebuild derived stores.
