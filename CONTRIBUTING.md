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

### Web

```powershell
cd app\web
npm ci
npm run dev
```

CI checks:

```powershell
npm run lint --if-present
npm test
npm run build --if-present
```

### API

```powershell
cd app\api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
uvicorn ai4ia_api.main:app --reload
```

CI checks:

```powershell
ruff check .
pyright
pytest -q
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

### Infra and operations

Use Bicep and schema validation for infra changes. Do not run `azd up`, `azd provision`, or `azd deploy` unless the maintainer explicitly asks. CI installs a pinned standalone [Bicep CLI](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install) release; if you don't have it locally but already have Azure CLI, substitute `az bicep build --file infra\main.bicep --stdout` (same output, no separate install).

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
- Use `.github/PULL_REQUEST_TEMPLATE.md` and wait for CODEOWNERS review where applicable.

## Reporting security issues

Do not open public issues for suspected vulnerabilities. Use GitHub private vulnerability reporting as described in [`SECURITY.md`](SECURITY.md).
