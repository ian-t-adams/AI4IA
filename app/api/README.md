# app/api — AI4IA Backend

FastAPI is the application's trust boundary: auth, user-scoped state, chat,
agents, tools, documents, memory, usage, and admin operations. Compatible
HTTP/SSE model calls use SimpleL7Proxy -> APIM -> Foundry. Realtime and Code
Interpreter use separately scoped APIM APIs; native service data planes are
distinct from model inference.

`AI4IA_MODEL_GATEWAY_URL` is the SimpleL7Proxy `/openai` URL for compatible
HTTP/SSE calls. When Voice Live is enabled, `AI4IA_REALTIME_BASE_URL` is the APIM
`/openai` URL used by the server-side WebSocket relay; it intentionally bypasses
SimpleL7Proxy.

## Responsibilities

- Auth: dev mode for local work, Entra validation for deployed environments,
  canonical internal user ids, and admin gates.
- Chat: sessions/history, streaming persistence, model selection, per-model token
  caps, optional rolling summarization, standing and one-turn agent routing,
  conversation tool/document selections, inspector snapshots, and slash commands.
- Agents/tools: curated agents, user-defined agents, workflows, governed built-in
  tools, generated image/video/document artifacts, Web IQ search tools when
  enabled, and user-registered remote MCP servers. The default-off
  `AI4IA_TOOL_AUTO_APPROVE_ENABLED` gate permits explicit session/run consent for
  enabled tools without bypassing execution authorization or losing activity and
  receipt evidence.
- Memory: disabled/in-memory/Cosmos backends; catalog-driven planning and
  embeddings; automatic recall; owner-scoped create/edit/delete; concurrency-safe
  forget; and atomic document memory replacement.
- Documents: per-session attachments plus the feature-gated cross-session library,
  Content Understanding ingest, retrieval, code interpreter, annotations, sharing,
  media playback metadata, and processing/export tools.
- Operations: usage ledger, entitlements, admin usage rollups, Azure Monitor
  resource panels, fixed bounded Log Analytics operations/security queries,
  structured metadata events, correlation ids, and Application Insights export
  when configured.

## Local dev

Run from `app/api`; use Python 3.12 to match CI and the container image.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
# First setup only; retain an existing local .env.
Copy-Item .env.example .env
python -m uvicorn ai4ia_api.main:app --port 8080 --reload
```

The web development proxy expects the API on port 8080. Local identity and
in-memory stores need no Azure setup; model calls still need a working gateway.

Run checks from this folder:

```powershell
ruff check .
pyright
pytest -q
```

The container image runs as non-root UID `10001`. Azure Container Apps is the
health authority: Bicep wires process-only `/health/live` liveness and cached
session-store `/health/ready` readiness probes. The image does not declare a
second Docker health policy that ACA ignores.

## Runtime image dependencies

The Docker build consumes the committed `uv.lock`, not the dependency ranges
alone. It checks freshness with `uv lock --check --offline` before frozen syncs;
a missing or stale lock fails the build with no automatic relock or pip fallback.
`--frozen` on its own would skip the freshness check.

Only runtime dependencies and the non-editable API package enter `/opt/venv`.
Dev and Foundry provisioning extras are not enabled; shared runtime dependencies
still remain. The final image copies that root-owned environment, not uv, build
caches, source inputs, or a workstation virtual environment, and runs as UID
`10001`. The build-only uv version must match `UV_VERSION` in `app-ci.yml`.

When changing dependencies, refresh the lock deliberately from public PyPI and
commit it with `pyproject.toml`; see [the contributor guide](../../AGENTS.md#frozen-api-runtime-dependencies).
Do not repair a failing build by dropping the freshness guard. The image build
checks package import and re-discovers lazy imports from the installed package
without installing any test tools in the image.

## Configuration posture

Feature flags are fail-closed in `ai4ia_api.config.Settings.validate_runtime`.
Local can use in-memory stores and fake clients; deployed environments must wire
durable stores, credentials, Origin allowlists, and real auth for the features
they enable. The [architecture](../../docs/architecture.md) explains the
boundaries; the authoritative flag list is
[`../../docs/runbooks/feature-enablement.md`](../../docs/runbooks/feature-enablement.md).

## Current gaps

- Memory has no global user-facing consent/toggle.
- Custom analyzer authoring is not surfaced.
