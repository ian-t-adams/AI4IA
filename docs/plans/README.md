# AI4IA roadmap — enhancement plans

This folder holds the grounded build plans for the next wave of AI4IA work. Each
plan is self-contained: it states the **current state** (verified against source),
the **target**, the **files to change**, the **default-OFF posture**, **tests**,
and **acceptance criteria**. Every feature ships behind a default-OFF flag and is
parked at the owner gate (no self-merge), consistent with repo governance.

## Workstreams

| # | Plan | Scope | Primary owner files |
|---|------|-------|---------------------|
| 1 | [VoiceLive enhancements](01-voicelive-enhancements.md) | Voice model picker, exposed session settings, enable tool calls | `app/web` voice components, `app/api` `routers/realtime.py`, `config.py` |
| 2 | [Context management + per-model settings](02-context-and-per-model.md) | Rolling summarization (long-chat), conversation/cross-session recall tool, per-model context-window + max-token scaling | `routers/chat.py`, `catalog.py`, `gateway/client.py`, `app/web` model/param UI |
| 3 | [Observability infra + telemetry](03-observability-infra-telemetry.md) | Diagnostic settings on all Azure resources → Log Analytics; App Insights / OpenTelemetry export from the API | `infra/modules/*.bicep`, `app/api` telemetry init, `pyproject.toml` |
| 4 | [Admin usage dashboard](04-admin-usage-dashboard.md) | Admin-only aggregation API over the usage ledger + resource metrics, and an admin dashboard UI | new `routers/admin_usage.py`, new `app/web` admin page |

## Why these four (and not five)

Workstreams 2 deliberately merges the *context-management* and *per-model-settings*
asks because both heavily edit the same hot path (`routers/chat.py`) and are
conceptually coupled — a model's context-window size is exactly what should drive
both the summarization threshold and the max-output-token cap. Keeping them in one
session avoids `chat.py`-against-itself merge conflicts and produces a coherent
"how much context goes in, how it's budgeted, and how output is capped per model"
change.

## Current state at a glance (verified against `main`)

These findings are the factual baseline each plan builds on.

- **Access model:** Single-tenant Entra. Any member of tenant
  `6df60a0a-…` who signs in has immediate full access — no invite, RBAC
  assignment, or entitlement provisioning needed (`auth/entra.py` validates
  tenant + signature only; `auth/userid.py` derives a stable id just-in-time;
  entitlements default to *unlimited*, deny only on explicit `disabled=True`).
  External (different-domain) people would need a B2B **guest** invite — that is
  the only "invite" case. An **admin concept already exists**
  (`auth/admin.py` `require_admin`, keyed on `AI4IA_ADMIN_SUBJECTS` /
  `AI4IA_ADMIN_EMAILS` or an Entra `admin` role claim).
- **VoiceLive:** voice picker ✅, smooth repeatable text↔voice with shared
  context ✅, tool calls built but **OFF** (`realtime_tools_enabled=false`),
  **model selection ❌ (hardcoded)**, **session settings ❌ (hardcoded)**.
- **Context:** full untruncated history sent every turn (no windowing, no
  summarization — `/summarize` is a stub); user-wide mem0 recall ✅
  (cross-conversation); no explicit conversation/session **search tool**.
- **Per-model:** max-tokens is user-adjustable (global 1–32000) but model
  entries carry **no context-window/max-output metadata** and there is **no
  per-model adaptation**; doc-context budget is fixed (8K–12K) for all models.
- **Observability:** Log Analytics + App Insights + Azure Monitor workspace are
  provisioned; Container Apps stream logs. **No diagnostic settings** on Cosmos,
  Postgres, AI Search, Storage, Event Hubs, or Key Vault. **No App Insights SDK**
  in the API. A durable **per-user usage ledger already exists** (Cosmos `usage`
  container: tokens/cost/model/agent/status; `GET /api/usage`). Event Hubs
  `telemetry` hub is provisioned but **dormant**. No admin usage endpoints / UI.

## Parallelization & recommended merge order

All four workstreams are developed in parallel on independent branches, each
opening its own PR, each **parked at the owner gate**. They are default-OFF and
independent, so merge order is flexible; the suggested order minimizes rebases:

1. **WS3 (observability infra + telemetry)** — mostly new infra + telemetry init;
   least overlap with app hot paths.
2. **WS1 (VoiceLive)** — isolated to voice files + `realtime.py`.
3. **WS2 (context + per-model)** — owns `routers/chat.py` and the chat/param UI.
4. **WS4 (admin dashboard)** — depends conceptually on WS3's telemetry being live
   for resource metrics, but the usage-ledger half can land independently.

Known light touch-points to resolve at merge (small, append-style):
`config.py` (WS1/WS2/WS3 each add settings), `app/api/.../main.py` (WS3 telemetry
init vs WS4 router registration), `app/web/.../ChatApp.tsx` (WS1 voice model
picker vs WS2 model/param settings).
