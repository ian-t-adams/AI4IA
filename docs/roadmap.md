# Roadmap & open items

A single, living list of what is **not yet done** — outstanding work, deliberate
tradeoffs, and owner decisions. It replaces the point-in-time audit reports that used
to live here; day-to-day architecture, config, and how-to material lives in the other
docs (see [`architecture.md`](./architecture.md),
[`configuration-reference.md`](./configuration-reference.md), and the
[runbooks](./runbooks/)). Keep this list short and current: delete an item when it
ships, add one when a real gap appears.

## Open items

| Priority | Item | What's needed | Tracked in |
| --- | --- | --- | --- |
| **P1** | **Deployment parity isn't automatically proven.** Repository truth can lead the live revision. | Keep recording deployed api/web/proxy revision SHAs + post-deploy smoke evidence after each deploy. | [`deployment.md`](./runbooks/deployment.md) |
| **P1** | **A second app cannot yet onboard onto the shared APIM/API Center.** Two things block it. (1) APIM **child entity** names are hardcoded to the `ai4ia-` prefix rather than derived from `${workload}` — `ai4ia-mcp` (`apimcore.bicep`, `mcpgateway.bicep`), `ai4ia-proxy-models`, `ai4ia-proxy-ingress`, `ai4ia-api-proxy-ingress`, `ai4ia-api-realtime`, `ai4ia-api-speech-voice-live` (`gateway.bicep`). A second workload collides on every one. `test_bicep_naming.py` only covers top-level globally-unique names, so nothing catches this. (2) `workload` is the one parameter with no `${AI4IA_…=default}` override (`main.parameters.json`), and `deploy.yml` hardcodes `rg="rg-ai4ia-${AZURE_ENV_NAME}"` plus `ca-web-`/`ca-proxy-` guards — so a renamed workload builds its RG somewhere the post-deploy custom-domain and cert steps never look, and they silently no-op. | Derive APIM child names from `${workload}`, add an `AI4IA_WORKLOAD` override, and derive the RG in the workflow. **Renaming live APIM products/subscriptions rotates the subscription key the proxy authenticates with, so this is a breaking change to the live gateway and needs its own reviewed, approved deploy — not a drive-by rename.** Extend `test_bicep_naming.py` to APIM children in the same change. | `infra/modules/apimcore.bicep`, `gateway.bicep`, `mcpgateway.bicep`, `deploy.yml` |
| **P1** | **Plain turns on a Responses-API model silently lose their tools.** `routers/chat.py:1599` gates the whole plain-path capability block on `... and api == "chat"`, so on a Responses-API model a turn with no tool-enabled agent gets **no web search, no document retrieval, and no compute** — and says nothing. Tool-*enabled* agents are handled correctly (a clear 422 at `chat.py:1023` says to choose a chat-completions model), so the gap is exactly the plain path. It bites the **Researcher** curated agent hardest: it ships `tools: []` (`data/agents.json`), so it always takes the plain path, and it is the one agent whose description promises to "gather, synthesize, and cite" — it will answer ungrounded from parametric knowledge with no indication grounding was unavailable. | Decide the posture, then implement: (a) surface a visible "grounding unavailable on this model" notice on the plain+Responses path — preferred, fixes the silence without regressing anything; or (b) give Researcher a real `tools` entry, which converts the silence into the existing loud 422 **but moves it onto the tool path, which is internally non-streaming, so it would lose true token streaming on chat-completions models**. Do not do (b) without accepting that tradeoff. | `routers/chat.py:1599`, `data/agents.json` |
| **P2** | **Long-running work has no durability.** Workflows execute **synchronously inside the HTTP request** (`routers/workflows.py`), and every other background job is an in-process `asyncio.create_task` (`routers/chat.py`, `library/ingest.py`). No Container Apps jobs are deployed. All of it dies with the replica on deploy, scale-in, or crash — with no retry, no dead-letter, and no operator visibility. Not speculative; it is the current design. | Move durable work off the request path. Best fit is **Durable Task SDKs + Durable Task Scheduler** (deliberately decoupled from compute, Python supported, runs on the Container Apps already deployed; fan-out/fan-in matches the researcher shape) or **Container Apps Jobs** for simple one-shots like document cracking. **Not Durable Functions** — it couples to Functions compute, adding a second platform, pipeline, and RBAC surface for no gain here. Note the large-payload option in Durable Task is .NET-only today. Any new compute must still route model calls proxy → APIM → Foundry per AGENTS.md rule 1. | `routers/workflows.py`, `routers/chat.py`, `library/ingest.py`, [`architecture.md`](./architecture.md) |
| **P2** | **`main` branch protection excludes the path-filtered workflows.** The ruleset requires the 11 always-emitted contexts (`quality` ×7, `codeql` ×2, `security-scan` ×2 blocking). `app-ci`, `infra-validate`, and `docker-build` are **deliberately not required**: they only run when their trigger paths match, and GitHub waits forever for a required check that is never reported — a docs-only PR would deadlock. Verified empirically: adding one unreachable context flipped an otherwise-green PR to `BLOCKED`. So a PR touching app or infra code shows its failure but is not *blocked* by it. | Add a small always-run aggregate job per path-filtered workflow that reports success when its real jobs are skipped, then require the aggregates. | Owner (repo settings), [`AGENTS.md`](../AGENTS.md) |
| **P2** | **Gateway is single-region / capacity-1** (APIM Basic v2) and the SimpleL7Proxy queue is per-replica memory (no global ordering/fairness). | Decide whether the capacity/region posture meets the target SLO and budget; set scaling + alert thresholds if not. | [`architecture.md`](./architecture.md) |
| **P2** | **Memory UX gaps.** No global memory-consent control and no recalled-memory provenance indicator; management is owner-scoped CRUD only. | Design explicit consent + provenance UX before expanding memory surfaces. | [`memory.md`](./memory.md) |
| **Ops** | **PostgreSQL retirement.** It is retained post-cutover only as the migration source + document-chunk fallback and for the rollback window. | After the agreed rollback window, remove the server, its firewall/admin, config, and data in a separate reviewed change — needs explicit destructive-action approval. | [`memory-migration.md` "Retirement"](./runbooks/memory-migration.md) |
| **Ops** | **Speech Voice Live final proof.** Both voice providers are enabled in production; the last open validation is a signed-in manual microphone canary. | Run the authenticated canary + manual retest and record correlated evidence. | [`deployment.md` §7.3](./runbooks/deployment.md), [`feature-enablement.md`](./runbooks/feature-enablement.md) |

## Accepted tradeoffs (decisions, not gaps)

- **Feature posture is intentionally expensive.** The checked-in live parameters enable
  several costly advanced surfaces (image/video generation, document understanding,
  search, both voice providers) by deliberate choice for this environment. See
  `infra/main.parameters.json`.
- **Basic v2 APIM is reused, not dedicated.** Reusing the existing paid service avoids a
  second ~$150/mo APIM base cost, at the cost of a shared blast radius across MCP,
  HTTP/SSE model, and both voice planes.

## Recently shipped (no longer open)

- **Proactive alerting is live** (verified in Azure 2026-08-02, not just wired):
  `AI4IA_ENABLE_ALERTS=true` with `AI4IA_ALERT_EMAIL=ian@nomad-analytics.com`, so
  action group `ag-ai4ia-slurmfactory` has a real recipient and both threshold
  rules (`alert-ai4ia-slurmfactory-api-5xx`, `alert-ai4ia-slurmfactory-cosmos-429`)
  are enabled. The `budget-ai4ia-slurmfactory` budget carries 50/80/100% notifications
  to the same mailbox. This entry records the *evidence*; delete it once it is stale.
- **`main` branch protection.** A repository ruleset now requires a pull request
  (with **0** required approving reviews, so a solo maintainer is not self-blocked),
  requires conversation resolution, blocks force-pushes and branch deletion, and
  requires the 11 always-emitted status checks. No bypass actors, so it applies to
  admins too; disabling it is a deliberate, visible settings change rather than a
  silent `git push`.
- **Legacy Consumption APIM removed** from Azure, the IaC, and the docs. The Basic v2
  `apim-mcp-*` service is now the only APIM plane; `gateway.bicep` creates no APIM
  service of its own.

- Canonical memory migrated **off mem0/PostgreSQL to Cosmos** and cut over in production
  (7 memories migrated + verified); full owner-scoped CRUD with ETags/idempotency.
- Staged-cutover deploy wiring (`AI4IA_MEMORY_STORE`) and the clean-room reproduction doc
  gaps (Entra app registrations, Cosmos vector-capability ordering) were closed.
- **New-tenant standup readiness.** Tenant-coupled defaults removed from the IaC and the
  operator scripts; subscription preflights added for resource-provider registration and
  per-subscription model availability. `scripts/provision-entra-apps.ps1` (dry-run-first)
  now creates both application registrations, exposes `access_as_user`, sets redirect URIs
  and admin consent, and prints the `AI4IA_ENTRA_*` values — the last manual portal step.
  Validated end to end against an empty subscription in a new tenant — see
  [deployment runbook §3](./runbooks/deployment.md).
- **Planet Express deployment live.** The stack is now running in
  `sub-planetexpress-slurmfactory` / `rg-ai4ia-slurmfactory` with custom domains for
  the app and proxy.
- **Gateway 4xx governance fixes.** Malformed Foundry 400s are no longer retried,
  healthy backends are not parked, terminal 4xx bodies survive APIM, and Responses API
  chat turns send `store=false` so Cosmos remains canonical.

[`CHANGELOG.md`](../CHANGELOG.md) tracks changes from this point forward. It does not yet
carry the project's earlier history: this repo has no reviewed release tags to backfill
from, and the changelog's own policy is not to invent them.
