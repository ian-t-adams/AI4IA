# Contributing

Thanks for improving AI4IA. Start with the canonical agent/contributor guide in [`AGENTS.md`](AGENTS.md), then use the runbooks under [`docs/runbooks`](docs/runbooks).

## Ground rules

- Keep compatible HTTP/SSE model traffic on SimpleL7Proxy -> APIM. Realtime
  WebSockets and Responses-API Code Interpreter are the two explicit exceptions
  in `AGENTS.md`; do not invent a third.
- Keep models catalog-driven from `infra/models.json`; do not hardcode deployment names.
- Keep feature gates server-authoritative and fail closed.
- Preserve per-user ownership, Cosmos canonical data, and tool SSRF/scope/approval checks.
- Do not commit secrets, generated local state, or deployment credentials.

## Local setup

### Prerequisites

| Tool | Supported version / purpose |
| --- | --- |
| Node.js | Version range in `app/web/package.json` (CI and the image use Node 22) |
| Python | 3.12, matching API CI and the container image |
| `uv` | Lock validation for `app/api/uv.lock` |
| .NET SDK | 10.x for the vendored proxy build/tests |
| Docker | Service image builds and `.dockerignore` boundary tests |
| Azure CLI + Bicep | IaC validation; `az bicep` is acceptable locally |
| Azure Developer CLI | 1.29.0 for the verified digest-deployment behavior |
| PowerShell | 7.x (`pwsh`), including on Linux/macOS because azd hooks use it |
| GitHub CLI | Greenfield workflow setup and operator commands in the runbooks |

Cloud deployment additionally requires the Azure RBAC, provider registration,
quota, Entra, OIDC, and cost configuration in the
[greenfield standup guide](docs/runbooks/greenfield-standup.md).

### Web

```powershell
cd app\web
Copy-Item .env.example .env
npm ci
npm run dev
```

CI checks:

```powershell
npm run lint --if-present
npm test
npm run build --if-present
```

#### Corporate npm mirrors

If the corporate mirror lacks locked versions and public npm is unreachable,
the failures have different causes:

| Command | Failure |
| --- | --- |
| `npm ci` (mirror) | **404**: the mirror is reachable but does not carry every lockfile-pinned version |
| `npm ci --registry=https://registry.npmjs.org` | **TLS handshake failure**: the public registry is unreachable |

Do not rewrite the repository lockfile or weaken TLS verification. A separate
scratch install can enable local checks, but it is **not lockfile-exact**; CI
remains authoritative for reproducibility. `npm install` also honors an existing
lockfile, so copy **only `package.json`** into the scratch directory to let the
resolver select versions the mirror carries.

From the repository root, with no existing `app\web\node_modules`:

```powershell
New-Item -ItemType Directory D:\ai4ia-web-scratch
Copy-Item app\web\package.json D:\ai4ia-web-scratch
Set-Location D:\ai4ia-web-scratch
npm install --no-audit --no-fund

# Replace <repo> with the checkout path; /node_modules is already gitignored.
cmd /c mklink /J <repo>\app\web\node_modules D:\ai4ia-web-scratch\node_modules
Set-Location <repo>\app\web
npm test
npm run lint
```

Keep Node 22. On a higher Node major, `ThemeProvider.test.tsx` can fail with
`localStorage is not available because --localstorage-file was not provided`.
That is an engine mismatch, not a product defect; do not change the tests.

**Turbopack cannot build through an external junction**, on any drive. It fails
during entrypoint discovery, before compiling source:

```text
Symlink [project]/node_modules is invalid, it points out of the filesystem root
  - Execution of find_package failed
```

For a local production build, remove only the checkout's junction, leaving its
scratch target intact, then copy the dependencies into a real directory:
`robocopy <scratch>\node_modules <repo>\app\web\node_modules /E /MT:16`.
Run `npm run build` there and remove the generated `app\web\.next` afterwards.
Passing `tsc --noEmit` and tests through the junction is useful but not equivalent
to a build: an extracted component missing `"use client"` can type-check and
still fail the production build.

### API

```powershell
cd app\api
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
uvicorn ai4ia_api.main:app --reload --port 8080
```

On Linux/macOS, use `cp .env.example .env` and
`source .venv/bin/activate`. The web example points `API_BASE_URL` at port 8080,
so keep the API command and web `.env` aligned.

CI checks:

```powershell
uv lock --check
ruff check .
pyright
pytest -q
```

CI also runs the Cosmos migration regression from the repository root after the
API development dependencies are installed:

```powershell
pytest -q scripts\tests\test_memory_cosmos_migration.py
```

From the repo root, also run catalog checks when catalogs, MCP manifests, Foundry manifests, or related scripts change:

```powershell
python scripts\gen-model-catalog.py --check
python scripts\gen-mcp-catalog.py --check
python scripts\gen-gateway-policy.py --check
python -m unittest scripts.tests.test_gateway_policy
python scripts\gen-voice-provider-catalog.py --check
python -m unittest scripts.tests.test_voice_provider_catalog
python scripts\validate-catalog.py
python scripts\validate-feature-prereqs.py
```

`AGENTS.md` is the authoritative CI inventory. It also lists the workflow-only
schema, Docker, shell/PowerShell, proxy, documentation, security, and operator
script gates; use that list rather than assuming the short commands above are
the whole required-check surface.

### Infra and operations

Use Bicep and schema validation for infra changes. Do not run `azd up`, `azd
provision`, or `azd deploy` against an existing shared environment unless its
owner explicitly asks. A first deployment must follow the greenfield guide; the
GitHub workflow performs release steps a local `azd up` does not. CI installs a
pinned standalone [Bicep CLI](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install)
release; if you do not have it locally but already have Azure CLI, substitute
`az bicep build --file infra\main.bicep --stdout`.

```powershell
check-jsonschema --schemafile infra\models.schema.json infra\models.json
check-jsonschema --schemafile infra\mcp-servers.schema.json infra\mcp-servers.json
bicep build infra\main.bicep --stdout
```

### Branding

The palette is orange + blue over near-black, and both stylesheets have contrast
gates because the failure mode is silent — the page renders, just illegibly.
Logos are generated from a single palette definition rather than hand-edited.

```powershell
python scripts\gen-brand-assets.py          # regenerate every committed raster
python -m unittest scripts.tests.test_portal_contrast   # site/assets/styles.css
python -m unittest scripts.tests.test_brand_assets      # image coverage, palette, size
cd app\web; npm test                        # includes globals.contrast.test.ts
```

See the "Change the brand palette or a logo" section of [`AGENTS.md`](AGENTS.md)
for the rules those gates encode.

## Branches and pull requests

- Branch from `main` unless coordinating a stacked change.
- Keep PRs focused and document user-visible behavior, config changes, and rollout risk.
- Include tests or a clear validation note for every behavior change.
- Update docs/runbooks when changing setup, flags, architecture, model catalog behavior, or operational posture.
- Use `.github/PULL_REQUEST_TEMPLATE.md`. CODEOWNERS routes matching changes;
  the ruleset requires a pull request and resolved review threads, not an approval.

## Reporting security issues

Do not open public issues for suspected vulnerabilities. Use GitHub private vulnerability reporting as described in [`SECURITY.md`](SECURITY.md).
