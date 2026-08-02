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
| **P1** | **A second app can now claim its own APIM credentials, but cannot yet onboard across subscriptions.** The naming collision is fixed: every APIM **child entity** name is derived from `${workload}` (`apimcore.bicep`, `gateway.bicep`, `mcpgateway.bicep`), `workload` has an `${AI4IA_WORKLOAD=ai4ia}` override, `deploy.yml` derives `rg-${AI4IA_WORKLOAD:-ai4ia}-${AZURE_ENV_NAME}`, and `ApimChildNamingTests` in `test_bicep_naming.py` fails any child name that reverts to a literal. The default resolves byte-identically to today's names, so no live subscription key rotated. What remains is genuine platform work: `apimcore.bicep` **creates** the APIM service, so a second app in another subscription has no way to reference the existing one; the `openai` and `realtime` **APIs** are deliberately shared rather than workload-derived (a second app reuses them, and the guard excludes them on purpose); and nothing registers a second app in API Center. | Split the shared plane from the consumer: a module that takes an **existing** APIM by resource id, and per-app product/subscription/policy attached to it. Adopt **APIM workspaces** at that point rather than a second instance — they already work on Basic v2, so federation does not by itself force the Standard v2 upgrade (private backends do). | `infra/modules/apimcore.bicep`, `gateway.bicep`, `mcpgateway.bicep`, [`architecture.md`](./architecture.md) |
| **P2** | **Durable execution now covers workflows but not the other background work.** `POST /api/workflows/{name}/run` accepts an opt-in `"durable": true` that runs the workflow on an Azure Durable Task Scheduler orchestration (`workflows/durable.py`). The flag is **enabled in production as of 2026-08-02**, so a scheduler is provisioned and a workflow no longer has to die with the replica. The rest of the background work is unchanged: `routers/chat.py` and `library/ingest.py` still use in-process `asyncio.create_task`, and no Container Apps jobs are deployed. Those sites are shielded, logged, and surface a `persistenceFailed` frame — so they are *observable*, not silent — but they are still not durable across a deploy or scale-in. | Move document cracking onto **Container Apps Jobs** (same image, identity, and networking; no new SDK). **Not Durable Functions** — it couples to Functions compute, adding a second platform, pipeline, and RBAC surface for no gain here. Any new compute must still route model calls proxy → APIM → Foundry per AGENTS.md rule 1. | `workflows/durable.py`, `routers/chat.py`, `library/ingest.py`, [`feature-enablement.md`](./runbooks/feature-enablement.md) |
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
- **Plain turns on a Responses-API model now say when grounding is unavailable.**
  `routers/chat.py` gates the plain-path capability block on `api == "chat"`, because
  this app's synthetic tool loop is implemented against the chat-completions wire
  format only. That is a real constraint, but it used to be **silent**. The turn now
  carries an explicit system notice telling the model its grounding/compute tools were
  unavailable for this request, so it answers honestly instead of presenting parametric
  recall as researched. This composes with the curated prompts, which already say to
  "be explicit that an answer is from model knowledge" — the model simply never knew
  which case it was in. Scope is narrower than first recorded: only **4 of 39** catalog
  models are `api: "responses"` (`gpt-5-pro`, `gpt-5.4-pro`, `gpt-5-codex`,
  `gpt-5.3-codex`), so Researcher is fully grounded on the other 35, including every
  gpt-5.6 daily driver. Rejected the alternative of giving Researcher real `tools`:
  it converts silence into a loud 422 but moves the agent onto the internally
  non-streaming tool path, losing true token streaming everywhere else.
- **The official-MCP plane is now verifiable.** `GET /api/admin/metrics/official-mcp`
  performs full MCP discovery (`initialize` → `tools/list`), not a ping — because the
  handshake returns 200 even when the upstream toolbox does not exist, so every
  ping-style check reported healthy while the plane served nothing.

[`CHANGELOG.md`](../CHANGELOG.md) tracks changes from this point forward. It does not yet
carry the project's earlier history: this repo has no reviewed release tags to backfill
from, and the changelog's own policy is not to invent them.
