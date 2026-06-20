# WS4 — Admin usage dashboard

**Goal:** give the app admin a dashboard answering "how many users, tokens used,
which models, how many agents, AI Search usage, Postgres usage, …" — built on the
existing admin gate and usage ledger, plus Azure Monitor for resource metrics.

## Current state (verified against `main`)

- ✅ **Admin gate exists:** `auth/admin.py` `require_admin` (keyed on
  `AI4IA_ADMIN_SUBJECTS` / `AI4IA_ADMIN_EMAILS` or an Entra `admin` role claim),
  already protecting `/api/admin/entitlements/*` (`routers/entitlements.py`).
- ✅ **Per-user usage ledger exists + is durable:** Cosmos `usage` container
  (PK `/userId`) with `promptTokens` / `completionTokens` / `totalTokens`,
  `estCostMicroUsd`, model, deployment, region, agent, status, `createdAt`
  (`usage/models.py`); written by `usage/service.py` `record_completion`. Served
  per-user by `GET /api/usage` (`routers/usage.py`).
- ❌ **No admin/org-level aggregation endpoints** (only the per-user summary).
- ❌ **No admin/dashboard UI** anywhere in `app/web`.
- Resource-level metrics (AI Search query volume, Postgres CPU/storage/connections,
  Cosmos RU, Container App replicas) live in **Azure Monitor / Log Analytics** —
  available for query once WS3 wires diagnostics (this plan can ship the
  usage-ledger half independently and add resource metrics when WS3 lands).

## Target

### A. Admin aggregation API (over the usage ledger)

New router `routers/admin_usage.py`, all gated by `require_admin`:

- `GET /api/admin/usage/summary?days=N` — org totals: active users, total
  tokens (prompt/completion), total estimated cost, request count, error rate.
- `GET /api/admin/usage/by-model?days=N` — tokens/cost/requests grouped by model.
- `GET /api/admin/usage/by-user?days=N` — per-user tokens/cost/requests
  (top-N + paging), joinable to entitlement limits.
- `GET /api/admin/usage/by-day?days=N` — daily time series for trend charts.
- `GET /api/admin/usage/agents?days=N` — usage attributed to agents (count of
  distinct agents used, per-agent token totals).

Implementation notes: aggregate by querying the Cosmos `usage` container
cross-partition (admin scope, not user-scoped) over the time window; reuse the
existing `UsageRecord` / `TokenUsage` models and cost fields. Keep queries bounded
(time window + top-N) to control RU. Best-effort, read-only.

### B. Resource-metrics API (over Azure Monitor — depends on WS3)

- `GET /api/admin/metrics/resources` — pull key platform metrics via the Azure
  Monitor query SDK (`azure-monitor-query`) using the API's managed identity:
  AI Search query volume + latency, Postgres CPU/storage/active-connections,
  Cosmos RU consumption, Container Apps replica/restart counts.
- Gracefully degrade (return "unavailable") if diagnostics/metrics aren't present
  yet, so this can ship before WS3 fully lands.

### C. Admin dashboard UI (Next.js)

- New admin route/page in `app/web` (e.g. `/admin`), shown only to admins —
  detect admin via a small `GET /api/admin/whoami` (or an `isAdmin` flag on the
  existing identity/me response) and hide the nav entry otherwise. Server still
  enforces `require_admin`; the UI hide is cosmetic.
- Cards + charts: total users / tokens / cost; tokens-by-model; tokens-by-day
  trend; top users (with their entitlement limits + a link to the existing
  entitlement admin actions); agents in use; AI Search & Postgres resource panels.
- Reuse the app's existing styling/components; keep it read-only except for the
  already-existing entitlement `PUT/DELETE` actions, which can be surfaced here.

## Files to change

- New `app/api/src/ai4ia_api/routers/admin_usage.py` (+ register in `main.py`).
- Possibly new `app/api/src/ai4ia_api/usage/aggregate.py` for the cross-partition
  aggregation queries (keep the router thin).
- `app/api/pyproject.toml` — add `azure-monitor-query` (for part B).
- `app/api/src/ai4ia_api/auth/...` or identity/me endpoint — expose an `isAdmin`
  flag for the UI (server gate unchanged).
- New `app/web/src/app/admin/…` (or equivalent route) + admin components.
- `app/web` nav — conditional admin link.

## Default-OFF / safety posture

- Every admin endpoint is behind `require_admin`; non-admins get 403. The UI link
  is hidden for non-admins but the server gate is the real boundary.
- Read-only aggregation (plus the pre-existing entitlement mutations); no new
  destructive operations.
- Aggregation queries are time-bounded + top-N to cap Cosmos RU and Monitor calls.
- No PII beyond what the admin already governs (user ids/emails already visible via
  entitlements admin).

## Tests

- API: `require_admin` enforced (admin 200, non-admin 403, anon 401) on every new
  route; aggregation math correct over a seeded fake usage store (by-model /
  by-user / by-day / totals); resource-metrics endpoint degrades gracefully when
  Monitor data is absent.
- Web: admin page renders for an admin identity and is hidden/blocked for a
  non-admin; charts render from a mocked API.

## Acceptance criteria

- An admin signs in and sees live counts: users, tokens (total + by model + by
  day), estimated cost, agents in use, and AI Search / Postgres resource panels.
- A non-admin cannot reach the page or the endpoints.
- Works on the existing usage ledger immediately; resource panels populate once
  WS3 diagnostics are deployed.
- `ruff check .` clean; `pytest -q` green except the known Windows flakes;
  web `npm test` + build green.

## Admin onboarding (owner action, for the runbook)

To become admin, set `AI4IA_ADMIN_EMAILS` (or `AI4IA_ADMIN_SUBJECTS`) to the
owner's tenant email/oid, **or** assign an Entra app `admin` role. No code change —
this is configuration on top of the existing `require_admin` gate.
