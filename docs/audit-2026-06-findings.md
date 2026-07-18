# AI4IA — Code & Docs Audit (2026-06)

**Scope:** whole repo — `app/api` (FastAPI, ~24K LOC), `app/web` (Next.js 16, ~13K
LOC), `infra` (Bicep), `proxy` (vendored SimpleL7Proxy), `scripts`, `.github`, `docs`.
**Mode:** find-and-note only. Nothing here is fixed; planning and repair happen
separately.
**Method:** six parallel read-only review passes (api-core, api-features, web,
infra, ci/cd, docs), then **every load-bearing claim was re-verified against the
source** before it earned a place in this list. Roughly a third of the raw
machine-generated findings were false or overstated and were rejected — see
[§8 Rejected findings](#8-rejected-findings-verified-false-or-overstated). The
counterpart to "be brutal" is "be accurate."

This audit is **layered on top of** `docs/brutal-audit.md`. Items that prior audit
already fixed, accepted, or tracked are **not** re-litigated as new — they are
listed once in [§9](#9-already-tracked-in-brutal-auditmd-not-re-flagged) so the two
documents do not fight.

---

## 1. Verdict

The core is genuinely well-engineered, and the brutality below should be read
against that baseline — this is a strong codebase with a **documentation-shaped
hole**, not a house on fire.

- **Security primitives are exemplary.** JWT validation (`auth/entra.py`) is strict
  and fail-closed; the SSRF guard (`agents/ssrf.py`) does fail-closed allowlisting,
  DNS-rebinding defense, and IP pinning; MSAL keeps tokens in `sessionStorage`; the
  web edge ships a nonce CSP and security headers.
- **The real debt is documentation and agent-enablement**, plus a short tail of
  correctness/hardening items. The single highest-leverage gap is that there is **no
  machine-facing contributor guidance at all** (`AGENTS.md` / copilot-instructions).
- **Shape, not safety, is the recurring code smell:** several 700–1200 LOC
  god-modules, thin web test coverage on the most complex surfaces, and a couple of
  inconsistencies (one real auth bug in `deleteDocument`).

---

## 2. How to read this

- **Severity** — `HIGH` = user-facing correctness/security or a top-priority gap;
  `MEDIUM` = real debt worth a scheduled fix; `LOW` = nit / cleanliness / hardening.
- **Tag** — `[NEW]` = surfaced by this audit; `[TRACKED]` = already in
  `brutal-audit.md`, repeated only for context.
- Every finding cites `path:line` so repair can start without re-discovery.

| # | Severity | Area | Finding |
| --- | --- | --- | --- |
| 3.1 | HIGH | Docs | No `AGENTS.md` / copilot-instructions anywhere |
| 3.2 | MEDIUM | Docs | Missing governance files (LICENSE, CONTRIBUTING, SECURITY, …) |
| 3.3 | MEDIUM | Docs | Diagram coverage thin; one excalidraw won't render on GitHub |
| 3.4 | MEDIUM | Docs | `app/web/README.md` misstates Node/runtime |
| 3.5 | LOW | Docs | API reference is Swagger-only, no narrative |
| 4.1 | HIGH | Web | `deleteDocument()` skips auth (`fetch` not `apiFetch`) |
| 4.2 | MEDIUM | Web | Megacomponents (ChatApp, VoiceLivePanel, voiceLive.ts) |
| 4.3 | MEDIUM | Web | No virtualization in `MessageList` |
| 4.4 | MEDIUM | Web | Thin test coverage on core surfaces |
| 4.5 | MEDIUM | Web | Accessibility gaps (focus trap, aria-labels) |
| 4.6 | LOW | Web | Proxy route: header forwarding + duplicated filter |
| 5.1 | MEDIUM | API | Unbounded Cosmos list queries |
| 5.2 | MEDIUM | API | God-modules (chat.py 1226 LOC, …) |
| 5.3 | LOW | API | `get_by_id` cross-partition, gate is contract-only |
| 5.4 | LOW | API | Systemic `except Exception` + hardcoded `/tmp` path |
| 6.1 | MEDIUM | Infra | No ACA liveness/readiness/startup probes in Bicep |
| 6.2 | MEDIUM | Infra | Foundry local (key) auth left enabled |
| 6.3 | LOW | Infra | Dead `abbreviations.json` |
| 7.1 | LOW | CI | Script robustness nits + token DRY |

---

## 3. Documentation & agent enablement — *the headline*

The docs that exist are good (`configuration-reference.md`, the runbooks, and
feature-enablement docs are 8–9/10). The problem is what is missing.

### 3.1 `[NEW]` `HIGH` — No `AGENTS.md` / copilot-instructions / CLAUDE.md

There is **zero** machine-facing contributor guidance in the repo. The load-bearing
invariants — *all model calls follow the approved gateway path*, *deployments are
catalog-driven*, *advanced surfaces are feature-gated and safe-by-default*, *Cosmos
is partitioned per user*, *every tool re-checks scope + SSRF allowlist* — are tribal
knowledge. An agent (or new human) editing this repo cannot discover them without
reading the whole tree. The user explicitly called out "agent items": this is the
top gap. Recommended outline for a root `AGENTS.md` (or `.github/copilot-instructions.md`):

```
1. What this repo is + the monorepo map (api / web / infra / proxy / scripts / docs)
2. The non-negotiable rules
   - HTTP/SSE model traffic goes SimpleL7Proxy -> APIM; realtime goes FastAPI relay -> APIM (never call Foundry direct)
   - No hardcoded model names — everything is catalog-driven (infra/models.json)
   - Feature gates are server-authoritative; never gate in the web app only
   - Cosmos is canonical + partitioned per user; derived stores are rebuildable
   - Tools re-check scope, approvals, and the SSRF allowlist at execution time
3. Build / test / lint commands per package (the exact ones CI runs)
4. How to add: a chat tool, a model, a feature flag, a router
5. Auth model (Entra prod / X-Dev-User local) and the apiFetch contract
6. Red flags — when to stop and ask a human
```

### 3.2 `[NEW]` `MEDIUM` — Missing standard governance / community files

The prior audit added `CODEOWNERS`. Still absent at the repo root:

- **`LICENSE`** — the repo is effectively unlicensed (only the vendored
  `proxy/SimpleL7Proxy` carries its own upstream license). For a Microsoft-authored
  repo this is the most important omission; nobody can legally reuse it.
- **`THIRD_PARTY_NOTICES.md`** — the proxy is vendored from upstream; its license
  and pinned commit should be acknowledged at root.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`.
- `.github/PULL_REQUEST_TEMPLATE.md` and `.github/ISSUE_TEMPLATE/`.

### 3.3 `[NEW]` `MEDIUM` — Diagram coverage is thin

Only **two** mermaid diagrams exist repo-wide (`docs/architecture.md` flowchart and
one in `docs/document-multimodal-understanding.md`). For a system with this many
moving parts there are **no sequence diagrams** for the flows that actually confuse
people:

- chat streaming (browser → Next proxy → API → gateway → Foundry, SSE back)
- auth (Entra prod vs `X-Dev-User` local; `apiFetch` token attach)
- the model gateway hop (SimpleL7Proxy queue/requeue -> APIM policy -> endpoint selection)
- Voice Live (browser ↔ API WebSocket, direct, bypassing the proxy)
- document ingestion / multimodal understanding

Also: `docs/architecture-overview.excalidraw` is a raw excalidraw JSON that **does
not render on GitHub** — either export an `.svg`/`.png` alongside it or inline a
mermaid version.

### 3.4 `[NEW]` `MEDIUM` — `app/web/README.md` misstates the runtime

`app/web/README.md:29-31` says *"CI and the production Docker image use Node 22, and
`package.json` declares that engine range."* Both halves are stale after the #90/#106
changes:

- the production image is **`node:26-alpine`** (`app/web/Dockerfile`), not Node 22;
- `engines.node` is now the **range `">=22.0.0 <27"`**, so "use Node 22" and "that
  engine range … fails early" is misleading — Node 23–26 locally will *not* fail.

This is exactly the "prose instead of fact" pattern `brutal-audit.md` warns about,
re-emerging in a doc the runtime bumps forgot to update.

> **Fixed (#187, follow-up round):** `app/web/Dockerfile` reverted to
> `node:22-alpine` and `engines.node` narrowed back to `">=22.0.0 <23"` — no
> runtime dependency (`next`, `typescript`, `eslint`, `vitest`) required Node 26;
> the original widening was manifest hygiene only, not a technical need. The
> README claim about `engines.node` "failing early" was also independently
> wrong (no `.npmrc`/`engine-strict` exists in this repo, so it only prints
> `npm warn EBADENGINE` and installs anyway) and has been corrected alongside
> the version fix. See the parallel `python:3.14-slim` incident in §9.

### 3.5 `[NEW]` `LOW` — API reference is Swagger-only

The API ships FastAPI's `/api/docs`, but there is no narrative API reference or
even a documented endpoint inventory. Swagger tells you *shapes*, not *contracts*
(auth required, feature-gate behavior, error envelope). A one-page
`docs/api-reference.md` keyed off the routers would close it.

---

## 4. Web app (`app/web`)

The web app is clean by the numbers: 0 `console.log`, 0 `dangerouslySetInnerHTML`,
0 `@ts-ignore`, 1 `any`, all `localStorage` access try/caught and limited to UI
prefs. The issues are correctness, shape, and reach — not hygiene.

### 4.1 `[NEW]` `HIGH` — `deleteDocument()` drops the auth token

`app/web/src/lib/api.ts:440-446`:

```ts
export async function deleteDocument(sessionId, documentId): Promise<void> {
  const resp = await fetch(                       // ❌ raw fetch — no bearer token
    `/api/sessions/${sessionId}/documents/${documentId}`,
    { method: "DELETE" },
  );
  ...
}
```

Every sibling call uses `apiFetch()`, which attaches the Entra bearer (see
`listDocuments` at `:432`). This one uses bare `fetch()`, so **under Entra auth no
`Authorization` header is sent** and the delete fails / lands in the wrong auth
context. It works in local dev only because the Next proxy injects `X-Dev-User`,
which is why it slipped past testing. Verified by reading both functions.

### 4.2 `[NEW]` `MEDIUM` — Megacomponents

Single files carrying too many responsibilities, hard to test or reason about:
`ChatApp.tsx` (791 LOC — sessions + messages + documents + voice + library + uploads
+ error/stream state), `VoiceLivePanel.tsx` (796), `lib/voiceLive.ts` (758 — audio
capture + PCM + WebSocket + SSE + turn state machine), `Composer.tsx` (731). Each
wants to be 3–5 focused units.

### 4.3 `[NEW]` `MEDIUM` — No virtualization in `MessageList`

`app/web/src/components/MessageList.tsx` renders every message in a flat list with
no windowing. Long chats (1000+ turns, each a rich subtree with citations /
attachments) hit a scroll-and-memory cliff.

### 4.4 `[NEW]` `MEDIUM` — Thin test coverage on the hardest surfaces

8 test files for 37 components. The harness and chat-path tests exist (per
`brutal-audit.md` #79, 90 tests pass), but the **most complex, most breakable**
surfaces are untested: `ChatApp`, `VoiceLivePanel`, the `apiFetch`/auth token flow,
the Next proxy route (`app/api/[...path]/route.ts`), `streamChat` SSE parsing, and
the Voice Live WebSocket lifecycle.

### 4.5 `[NEW]` `MEDIUM` — Accessibility gaps

Modal dialogs (e.g. `StudioPanel.tsx`) set `role="dialog" aria-modal="true"` but have
**no focus trap** — Tab escapes to background content. Several icon/emoji controls
(send, attach, voice toggle in `Composer.tsx` / `VoiceLivePanel.tsx`) lack
`aria-label`. Directional (not exhaustively swept).

### 4.6 `[NEW]` `LOW` — Proxy route hardening

`app/web/src/app/api/[...path]/route.ts`: (a) no upstream `fetch` timeout (`:58-66`)
— a hung backend hangs the edge; (b) no `..` path-segment normalization (`:27`);
(c) request headers are forwarded wholesale minus hop-by-hop, so a browser could
inject `Authorization`/other headers (low risk — `x-dev-user` is overwritten and
MSAL tokens aren't auto-sent — but worth sanitizing); (d) the hop-by-hop filter is
copy-pasted for request and response (`:30-40`, `:68-77`) — extract a helper.

> **Rejected here:** the "streaming reader leaks memory on abort" claim. The abort
> path calls `controller.abort()` (`api.ts:708`), which cancels the response body
> stream per spec; the missing explicit `reader.cancel()` is cosmetic. See §8.

---

## 5. API (`app/api`)

Hygiene is strong: 0 `print`, 0 bare `except`, 0 `type: ignore`, 1 TODO. The
findings are scalability, shape, and one defensible-but-fragile contract.

### 5.1 `[NEW]` `MEDIUM` — Unbounded Cosmos list queries

`app/api/src/ai4ia_api/library/cosmos_repo.py:74-138`: `list_documents`,
`list_shared_with`, and `list_by_status` materialize **all** matching rows with no
`LIMIT` and no continuation token. The blast radius is bounded (queries are scoped
to a per-user partition), but a heavy library means unbounded latency and RU cost on
a hot path. Add paging / a cap.

### 5.2 `[NEW]` `MEDIUM` — God-modules

`routers/chat.py` **1226 LOC** (streaming chat core), `routers/library.py` 960,
`gateway/client.py` 842 (a ~610-line `ModelGatewayClient`), `config.py` 795,
`realtime.py` 754. These concentrate risk and resist unit testing. Same medicine as
the web megacomponents: decompose along the seams.

### 5.3 `[NEW]` `LOW` — `get_by_id` is cross-partition with a contract-only gate

`cosmos_repo.py:104-118` does a cross-partition read and authorizes purely via a
docstring (*"caller MUST gate"*). It's correct today, but one forgetful future
caller turns it into a cross-user read. Consider taking an explicit owner/user
parameter so the gate is enforced by the type signature, not by prose.

### 5.4 `[NEW]` `LOW` — Defensive-exception sprawl + predictable scratch path

146 `except Exception` sites (142 carry `# noqa: BLE001`). The vast majority are
deliberate cleanup/telemetry handlers logging `exc_info=True` — defensible — but 6
are silent `pass` (all telemetry). Worth a periodic grep so genuinely-swallowed
errors don't hide in the pattern. Separately, `config.py:522` hardcodes
`/tmp/mem0_history.db` (`# noqa: S108`) — predictable, world-readable temp path;
prefer `tempfile`/a configured dir.

> **Rejected here:** "audience-validation substring bug" in `auth/entra.py` (it's
> exact `set` membership), "MCP client leak on SSRF failure" (the pin check runs
> before the client is created), "entitlements fail-open bypass" (documented
> deliberate posture with a startup guard), and "pricing float CRITICAL" (single
> round per independent estimate, no accumulation). All false/overstated — see §8.

---

## 6. Infrastructure (`infra`)

The infra is solid where it counts: storage `allowSharedKeyAccess:false` /
`allowBlobPublicAccess:false`, Cosmos/EventHubs/Search `disableLocalAuth:true`, a
`dataTierPrivate` flag to privatize the data tier, and api/web `minReplicas:1`. The
genuinely-new gaps:

### 6.1 `[NEW]` `MEDIUM` — No ACA health probes wired in Bicep

A grep for `probes`/`livenessProbe`/`readinessProbe`/`startupProbe` across **every**
Bicep module returns **zero** hits. The app exposes purpose-built `/health/live` and
`/health/ready` endpoints, and the API image has a Docker `HEALTHCHECK` (added by the
prior audit), but the Container Apps deployment never wires those endpoints as ACA
liveness/readiness/startup probes. Result: ACA falls back to default TCP checks and a
revision can take traffic before the app reports ready. `infra/modules/api.bicep`,
`web.bicep`, `gateway.bicep`.

### 6.2 `[NEW]` `MEDIUM` — Foundry local (key) auth left enabled

`infra/modules/foundry.bicep:15` defaults `disableLocalAuth = false`, and
`infra/main.bicep` does **not** override it to `true`; `publicNetworkAccess` is
hardcoded `Enabled` (`:36-37`). The gateway authenticates with managed identity, so
account keys are a *latent alternate path*, not an active exposure — but leaving
local auth on is a hardening gap on the resource that authorizes every governed
Foundry model call.
Note: this is **not** in `brutal-audit.md`'s accepted list. (Distinct from the
already-accepted public-network-access posture on other planes.)

### 6.3 `[NEW]` `LOW` — Dead `abbreviations.json`

`infra/abbreviations.json` (azd scaffolding) is referenced **0 times** across the
infra tree. Either wire it into naming via `loadJsonContent('./abbreviations.json')`
or delete it. Lean-and-clean nit.

> **Not re-flagged:** Postgres HA, Cosmos PITR / zone redundancy, Key Vault purge
> protection, APIM token-limit policies, gateway `minReplicas:0` cold-start, and
> app-layer rate-limiting/upload quotas are all **already documented** in
> `brutal-audit.md` as deliberately-deferred cost/reliability decisions. See §9.

---

## 7. CI/CD, scripts & proxy

CI/CD is in good shape post-hardening: every `uses:` is SHA-pinned, jobs have
`timeout-minutes`, Dependabot + CodeQL are wired, and `deploy.yml` uses `vars.*`
(not `secrets.*`) for OIDC. Remaining items are small.

### 7.1 `[NEW]` `LOW` — Script robustness + token DRY

- **Naming-token duplication.** `slurmfactory` (a documented subscription naming
  token — *not* a secret) is hardcoded independently in `scripts/gen-model-catalog.py:30`
  and `scripts/validate-catalog.py:54`, which must agree or deployment names drift
  silently. Share one constant / load it from `infra/models.json`.
- **Unguarded `json.loads`.** `gen-model-catalog.py`, `validate-catalog.py`, and
  `validate-feature-prereqs.py` parse JSON with no try/except — malformed input
  yields a cryptic stack trace instead of an actionable message.
- **`.Count` on a scalar.** `scripts/seed-models.ps1:28` —
  `(... | ConvertFrom-Json).Count` returns `$null` for a single-object JSON; wrap in
  `@(...)` to normalize.
- **Validator embeds the identifiers it forbids.** `scripts/validate-feature-prereqs.py:95,97`
  hardcodes `ian-t-adams` / `ianadams@microsoft.com` to *block* personal defaults
  shipping. Intentional and defensive — but the forbidden values live in the repo;
  a config list would read better. Not a leak.

> **Already tracked (not re-flagged):** "no PR-time `docker build` of either
> Dockerfile" was in `brutal-audit.md`'s **Known open items** and is now **fixed**
> (`.github/workflows/docker-build.yml`, #187). The `eslint-config-next` /
> eslint-10 block once listed alongside it is also **resolved** (#124 native
> flat-config rework + #132 eslint 10), though `npm ci` still prints benign
> `ERESOLVE overriding peer dependency` warnings for `eslint-config-next`'s
> bundled plugins (their published peer ranges cap at `eslint@^9`/`^9.7` as of
> this writing — install still exits 0 and lint/build/test are unaffected).
> The CI-vs-image runtime skew (CI Node 22 / Python 3.12 vs `node:26` / `python:3.14`)
> was a direct consequence of that same no-PR-docker-build gap (the Python 3.14 bump
> was instead validated via `uv pip compile`); #187 now build-validates both images
> on every PR. **Update:** the skew itself is also now fixed, not just detectable —
> both `app/web/Dockerfile` (`node:26-alpine` → `node:22-alpine`) and
> `app/api/Dockerfile` (`python:3.14-slim` → `python:3.12-slim`) were reverted to
> the CI-tested majors after confirming neither had a genuine dependency
> requirement for the newer major. See §9.

---

## 8. Rejected findings (verified false or overstated)

Recording these so repair effort isn't wasted chasing non-issues, and as evidence
the list above was filtered, not dumped.

| Claimed | Reality | Where |
| --- | --- | --- |
| `CRITICAL` JWT audience accepts substrings | `_accepted_audiences` is a `set`; `a in set` is exact membership. | `auth/entra.py:117` (set built `:43-48`) |
| MCP client leaked on SSRF failure | `_pin_or_raise()` runs **before** `_new_client()`; nothing is created to leak on a validation failure. | `agents/mcp_client.py:188-202` |
| Entitlements "fail-open bypass" | Docstring documents "fail-open except disabled" as a deliberate choice for a personal/demo app, backed by a `validate_runtime` startup guard. Design posture, not a hole. | `entitlements/service.py:1-21` |
| `CRITICAL` pricing float precision | One round-to-micro-USD per independent estimate; no cross-entry accumulation to drift. | `usage/pricing.py:75` |
| "Blanket pyright suppression hides bugs" | Narrowly-scoped, well-commented suppression explaining Cosmos SDK type invariance. Acceptable. | `library/cosmos_repo.py:1-6` |
| Streaming "memory leak on abort" | `controller.abort()` cancels the body stream per spec; explicit `reader.cancel()` is cosmetic. | `lib/api.ts:699-708` |
| "Hardcoded subscription token (CRITICAL supply-chain)" | `slurmfactory` is a **documented naming token** (`docs/naming-and-tagging.md`, `main.bicep`, `model_catalog.json`), not a secret. Real issue is only DRY (see 7.1). | repo-wide |
| MSAL `sessionStorage` "risk" | This is the **correct, safer** choice vs `localStorage`. Not a finding. | `lib/auth.ts:56` |

---

---

## 9. Already tracked in `brutal-audit.md` (not re-flagged)

These are real, but `docs/brutal-audit.md` already records them as **fixed**,
**accepted**, or **tracked open**. Listed so this audit doesn't double-count them.

| Item | Status in brutal-audit |
| --- | --- |
| No PR-time `docker build` of either Dockerfile | **Fixed** (#187) |
| `eslint-config-next` `^16` / eslint 9→10 block | **Resolved** (#124/#132); benign `npm ci` peer-warning noise from bundled plugins remains, tracked as harmless upstream noise |
| CI-vs-image runtime skew (`node:26-alpine` / `python:3.14-slim` vs CI's Node 22 / Python 3.12) | **Fixed** (#187) — both Dockerfiles reverted to the CI-tested majors; neither had a genuine dependency requirement for the newer one |
| `style-src 'unsafe-inline'` in CSP | **Deliberate** documented relaxation (#101) |
| Gateway proxy `minReplicas:0` cold-start | **Accepted** cost tradeoff |
| Cosmos PITR / KV purge protection / Postgres HA | **Deferred** cost/reliability decision |
| APIM token-limit policies, app rate-limiting, upload quotas | **Deferred** decision |
| API image had no container health check | **Fixed** — Docker `HEALTHCHECK` added |
| Personal IaC owner/email defaults | **Fixed** — neutralized; the `validate-feature-prereqs.py` guard enforces it |
| Expensive feature posture in live params | **Accepted** by design |

---

## 10. Prioritized backlog (for the separate planning pass)

Ordered by leverage, not just severity. **No fixes are applied here.**

1. **Add `AGENTS.md` / `.github/copilot-instructions.md`** (3.1) — highest leverage;
   unlocks safe agent + human contribution. Outline provided.
2. **Fix `deleteDocument()` auth** (4.1) — only real user-facing correctness bug;
   one-line change (`fetch` → `apiFetch`) but ship with a regression test.
3. **Add `LICENSE` + `THIRD_PARTY_NOTICES.md`** (3.2) — legal blocker for reuse.
4. **Wire ACA readiness/liveness probes** (6.1) and **disable Foundry local auth**
   (6.2) — small Bicep changes, real reliability/hardening wins.
5. **Bound the Cosmos list queries** (5.1) — paging before a library gets big.
6. **Decompose the god-modules** (4.2 / 5.2) and **add tests** for the now-testable
   units (4.4) — pairs naturally; do them together.
7. **Add sequence diagrams + fix the excalidraw render + correct the web README**
   (3.3 / 3.4) — cheap, high-readability.
8. **Sweep the LOW tail** (4.3, 4.5, 4.6, 5.3, 5.4, 6.3, 7.1) — opportunistic.

---

## 11. What's genuinely good (so the brutality is calibrated)

- **Auth:** `auth/entra.py` JWT validation is strict, fail-closed, exact-audience.
- **SSRF:** `agents/ssrf.py` is a reference-quality guard (allowlist + DNS-rebinding
  defense + IP pinning, all fail-closed).
- **Edge:** nonce-based CSP + full security-header set; MSAL in `sessionStorage`.
- **Architecture:** centralized model gateway, catalog-driven deploys, server-
  authoritative feature gates, per-user Cosmos partitioning, rebuildable derived
  stores — and an honest, well-maintained `brutal-audit.md` trail.
- **Hygiene:** API has 0 `print`/0 bare-except/0 `type:ignore`; web has 0
  `console.log`/0 `dangerouslySetInnerHTML`/0 `@ts-ignore`. Supply chain is
  SHA-pinned with Dependabot + CodeQL.

The gap between this codebase and "excellent" is mostly **documentation, diagrams,
agent-enablement, and decomposition** — not security or correctness.

---

*Audit date: 2026-06-24 · Branch: `ian-t-adams-code-review-audit` · Method:
six read-only review passes, every load-bearing claim re-verified against source.
Find-and-note only — no code changed.*
