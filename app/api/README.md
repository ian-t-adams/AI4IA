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
  tools, generated image/document artifacts, legacy read-only video artifacts, Web IQ search tools when
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

The official digest-pinned Python 3.12 Alpine base replaces Debian slim to avoid
unfixed HIGH/CRITICAL Debian findings. Pinned APK updates repair the base's
krb5 findings and retain the complete package inventory; `libstdc++` supplies
native wheel dependencies and `tzdata` preserves system TZif calendar rules.
The dependency-only sync refuses
source builds: every locked runtime dependency must have a supported wheel for
the target platform. No glibc shim or alternative package vendor is used.
The installed protobuf release supplies a supported pure-Python wheel on musl;
CI exercises serialization as well as native gRPC and cryptography imports.

PR image CI checks stdlib compression/SQLite/TLS, SDK imports, workflow calendars,
non-root ownership, the default command with a non-default port, loopback-only
health probes with networking disabled, and graceful SIGTERM. `exec` keeps
Uvicorn as PID 1 while retaining `/bin/sh` for Container Apps exec. Both final service
images are scanned for all HIGH/CRITICAL findings, including unfixed findings,
with no ignore file; their vulnerability JSON and complete SPDX SBOMs are retained
even when the scan fails. A base-only scan is not final-image acceptance.

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
