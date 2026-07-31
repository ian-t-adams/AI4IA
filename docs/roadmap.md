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
| **P0** | `main` has **no branch protection / ruleset** (verified: protection `404`, rulesets `[]`). Any direct push bypasses CI and review. | Add a ruleset requiring a PR + one approving review + conversation resolution + blocked force-push/deletion, and require **always-emitted aggregate** check contexts (not the path-filtered `app-ci`/`infra-validate`/`docker-build` job names, which would deadlock docs-only PRs). | Owner (repo settings) |
| **P1** | **Proactive alerting is wired but off.** `enableAlerts=false` by default; only App Insights Smart Detection is live, so there are no threshold alerts for API 5xx spikes or Cosmos throttling. | Plumbing is done: set the `AI4IA_ENABLE_ALERTS` repo variable to `true` **and** `AI4IA_ALERT_EMAIL` to a recipient, then redeploy. Reactive diagnosis via the admin dashboard already works. | `infra/modules/alerts.bicep`, `deploy.yml`, [`telemetry.md`](./runbooks/telemetry.md) |
| **P1** | **New-tenant standup is now scripted.** Creating the two application Entra app registrations (API + web SPA) used to be a manual portal/CLI step. | `scripts/provision-entra-apps.ps1` (dry-run-first) creates both apps, exposes `access_as_user`, sets redirect URIs + admin consent, and prints the `AI4IA_ENTRA_*` values. | [`deployment.md` §2.7](./runbooks/deployment.md), `scripts/provision-entra-apps.ps1` |
| **P1** | **Deployment parity isn't automatically proven.** Repository truth can lead the live revision. | Keep recording deployed api/web/proxy revision SHAs + post-deploy smoke evidence after each deploy. | [`deployment.md`](./runbooks/deployment.md) |
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

- Canonical memory migrated **off mem0/PostgreSQL to Cosmos** and cut over in production
  (7 memories migrated + verified); full owner-scoped CRUD with ETags/idempotency.
- Staged-cutover deploy wiring (`AI4IA_MEMORY_STORE`) and the clean-room reproduction doc
  gaps (Entra app registrations, Cosmos vector-capability ordering) were closed.
- **New-tenant standup readiness.** Tenant-coupled defaults removed from the IaC and the
  operator scripts; subscription preflights added for resource-provider registration and
  per-subscription model availability. Validated end to end against an empty subscription
  in a new tenant — see [deployment runbook §3](./runbooks/deployment.md).

[`CHANGELOG.md`](../CHANGELOG.md) tracks changes from this point forward. It does not yet
carry the project's earlier history: this repo has no reviewed release tags to backfill
from, and the changelog's own policy is not to invent them.
