# Brutal Repo Audit

This repo is ambitious, useful, and dangerously tolerant of operator pain. The
architecture is not the embarrassing part; the embarrassing part is how many
ways the setup can look green while the deployment is stale, mis-owned,
half-configured, or expensive.

## Verdict

AI4IA is a serious Azure AI workload wearing a demo repo hoodie. It has real
governance ideas: SimpleL7Proxy -> APIM in front of HTTP/SSE model calls, managed identity,
feature gates, Cosmos as canonical state, rebuildable derived stores, and
Container Apps for deployable services. Good.

Now the yelling: the repo used to ship personal IaC defaults, tolerate
`npm install` in production image builds, document custom-domain outages as if
that made them less outage-shaped, and rely on prose instead of CI for obvious
configuration contradictions. Documentation existed, but it was scattered
enough that an operator had to play archaeology with `main.bicep`,
`main.parameters.json`, workflow variables, app env, and runbooks.

## What was fixed in this audit

| Area | Before | Fixed |
| --- | --- | --- |
| Web builds | Docker and CI could fall back from `npm ci` to `npm install`, destroying reproducibility. | Docker and CI now use deterministic `npm ci`. (Docker base was later bumped Node 22→26 via #90; see "Known open items" — `engines.node` was not widened to match.) |
| Web runtime | No route-level error boundary, so a client render crash could dump users into default framework failure UI. | Added `app/web/src/app/error.tsx` with a recoverable error screen. |
| API image | Non-root runtime existed, but the image had no container-native health check. | Added a Docker `HEALTHCHECK` against `/health/live`. |
| IaC ownership | Personal owner/email defaults leaked into tags, budgets, and APIM publisher config. | Replaced personal defaults with neutral deployment-owned defaults and wired `AI4IA_APIM_PUBLISHER_EMAIL`. |
| Infra validation | CI validated Bicep syntax and model catalog shape, not contradictory deployment settings. | Added `scripts/validate-feature-prereqs.py` to `infra-validate.yml`. |
| Config docs | Feature/env/parameter mapping was scattered. | Added `docs/configuration-reference.md`. |

## Resolved after the audit

Each item below shipped as an independent, validated follow-up PR after the audit
baseline (#78) merged. The audit's "next fixes" backlog is now cleared.

| Problem | Resolution | PR |
| --- | --- | --- |
| Component tests were basically absent. | Stood up a jsdom + React Testing Library harness and added real behavior tests for the chat path (Composer, MessageList, ModelPicker); 90 web tests now pass. | #79 |
| API error responses were inconsistent. | Added a shared error module with a consistent `{detail, code, correlation_id}` body and swept all 8 routers onto `status.HTTP_*`. | #81 |
| Type checking was missing from API CI. | Added pyright (basic mode, 0 errors from a 72-error baseline) as a required CI step between ruff and pytest. | #82 |
| Post-provision was mostly a reminder. | Replaced it with smoke tests that hard-gate on model deployments and check API health and DNS, failing non-zero on regressions. | #80 |
| Custom domains were externally fragile. | Added a read-only `deploy.yml` preflight that refuses to provision when a live vanity binding exists but its CI variable is empty. | #80 |
| Web lint under-covered the TypeScript sources.\* | Added `next/typescript` (`@typescript-eslint/recommended`) to the flat config and cleared the two dead-code warnings it surfaced. | this PR |

\* Not flagged in the original audit. While addressing it, the suspected
"`eslint.config.mjs` is missing react-hooks" gap turned out to be overstated:
`next/core-web-vitals` already enables `plugin:react-hooks/recommended`
(rules-of-hooks = error, exhaustive-deps = warn), and for Next.js 15 the
`FlatCompat` bridge is the framework-recommended way to consume
`eslint-config-next` (it has no native flat-config export yet), not a legacy
shim to rip out. The real, defensible win was layering TypeScript-aware rules on
top — a reminder that even an audit's own findings deserve verification before
"fixing."

## Remaining / accepted tradeoffs

| Item | Status | Where to look |
| --- | --- | --- |
| Feature posture is expensive by design. | **Accepted.** The checked-in live parameters intentionally enable several costly advanced surfaces. This is a deliberate, acceptable choice for this environment — documented here so it reads as a decision, not an oversight. | `infra/main.parameters.json` |

## First-party guidance this critique is grounded in

- [Azure Well-Architected Framework](https://learn.microsoft.com/azure/well-architected/what-is-well-architected-framework): workloads should balance reliability, security, cost, operational excellence, and performance. This repo is strongest on architecture intent and weakest on operational proof.
- [Azure Developer CLI GitHub Actions pipeline](https://learn.microsoft.com/azure/developer/azure-developer-cli/pipeline-github-actions): `azd` pipelines are meant to provision and deploy automatically. A green pipeline that cannot prove the app works is not enough.
- [API Management AI gateway capabilities](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities): APIM can govern model access, token limits, resiliency, and observability. AI4IA has the gateway pattern; it still needs stronger documented policy posture around token quotas and runtime dashboards.
- [Azure Container Apps architecture best practices](https://learn.microsoft.com/azure/well-architected/service-guides/azure-container-apps): Container Apps workloads need explicit reliability, health, revision, ingress, security, and observability decisions. The repo had probes in app routes, but the API image itself did not advertise a health check.

## Status of the next fixes

1. ✅ API type checking in CI with a realistic baseline (pyright basic, 0 errors). — #82
2. ✅ Normalized API error response shape; no raw integer status codes. — #81
3. ✅ DOM/component test harness for the web chat path. — #79
4. ✅ Post-provision reminder replaced with smoke tests that fail loudly. — #80
5. ✅ Custom-domain deploy validation that refuses to proceed when an expected vanity hostname is missing from CI variables. — #80

## Second pass: supply-chain & security hardening

A later sweep focused on the CI/CD supply chain and the web edge — the parts that
were "fine" only because nothing had gone wrong yet.

| Problem | Why it matters | Fix |
| --- | --- | --- |
| GitHub Actions were referenced by mutable tags (`@v4`, `@v2`). | A moved tag (compromised or just changed) silently runs new code with repo/OIDC access. | Pinned every `uses:` to a full commit SHA, with the version in a trailing comment. |
| No automated dependency or action updates. | Patches (including security fixes) landed only when someone remembered. | Added `.github/dependabot.yml` for npm, pip, github-actions, and Docker base images (weekly, grouped). |
| No SAST. | A repo with auth, a proxy, and file uploads had zero static analysis. | Added `.github/workflows/codeql.yml` (python + javascript-typescript, weekly + PR). |
| CI jobs had no timeouts or concurrency control. | A hung job could run for 6 hours; superseded PR pushes stacked redundant runs. | Added `timeout-minutes` to every job and `cancel-in-progress` concurrency to the validate workflows. |
| The web app set no security response headers. | No clickjacking, MIME-sniffing, referrer, or HSTS protection at the edge. | Added a conservative `headers()` in `next.config.mjs` (nosniff, frame-ancestors/X-Frame-Options DENY, Referrer-Policy, HSTS, scoped Permissions-Policy, plus the non-breaking CSP directives). A full nonce-based script CSP is deliberately left as a separate app-aware change. |
| No ownership map. | Reviews were not routed; no record of who owns what. | Added `.github/CODEOWNERS`. |

Deliberately **not** changed in this pass (flagged as cost/reliability decisions for
the owner, not silent edits): APIM token-limit/metric policies, proxy `minReplicas`
cold-start, Cosmos PITR / Key Vault purge protection / Postgres HA, and app-layer
rate limiting / upload quotas.

## Third pass: dependency currency, edge CSP & API pinning

After the supply-chain scaffolding (Dependabot, CodeQL, SHA pins) was in place, the
bot immediately started producing PRs — which is the point. This pass triaged the
full Dependabot backlog and shipped several optional improvements. Everything below
landed on `main` **except** #92 (genuinely blocked upstream — see open items).

| Update | Disposition | PR |
| --- | --- | --- |
| GitHub Actions group (5 actions: checkout, setup-node, setup-python, codeql-action, azure/login). | **Merged.** SHA-pin/comment-only bumps; no input or breaking changes. | #93 |
| `@types/node` 20→26 (devDep). | **Merged.** No code changes needed. | #97 |
| `@azure/msal-react` 2→5 + `@azure/msal-browser` 3→5 (coupled). | **Merged** as one. Required two in-scope `src/lib/auth.ts` fixes for msal-browser v5 removals (`navigateToLoginRequestUrl` now per-request; `storeAuthStateInCookie` dropped from `CacheOptions`) — both were default values, so behavior is unchanged. | #95 (#98 closed as superseded) |
| `typescript` 5→6 (devDep). | **Merged.** Added `src/css.d.ts` (`declare module "*.css"`) for TS6's stricter side-effect import resolution and annotated `int16ToFloat32`'s return for TS6's now-generic `TypedArray`. | #99 |
| `next` 15→16 (riskiest). | **Merged.** Built/tested clean against `main` with zero app code changes. Surfaced the `middleware`→`proxy` and `next.config` `eslint`-key deprecations (addressed / noted below). | #96 |
| `python` 3.12-slim → 3.14-slim (API image). | **Merged — with evidence, not faith.** No PR CI job builds this Dockerfile, so green CI was *not* base-image proof. Validated instead with a wheels-only resolution: `uv pip compile --python-version 3.14 --python-platform x86_64-unknown-linux-gnu --no-build` resolved the full 109-package tree (incl. `mem0ai → qdrant-client → grpcio/numpy`) at exit 0, proving every C-extension has a prebuilt cp314 wheel. | #91 |
| `node` 22-alpine → 26-alpine (web image). | **Merged.** Left `app/web/package.json` `engines.node` at `">=22.0.0 <23"`, contradicting the image — since fixed in #106 (widened to `">=22.0.0 <27"`). | #90 |
| `mem0ai` bump (API). | **Merged.** | #94 |
| `eslint` 9→10 (devDep). | **Resolved (#132).** Previously held at eslint 9 (blocked upstream). Unblocked once `eslint-config-next@16.2.10` loosened its eslint peer to `>=9.0.0` (which includes 10) and CI's Node 22 satisfied eslint 10's engine (`^20.19\|\|^22.13\|\|>=24`). One config fix needed: pinned `settings.react.version = "19.2.7"` in `eslint.config.mjs` (eslint 10 removed `context.getFilename()`, which eslint-plugin-react's `version:"detect"` path relied on). No rules disabled, no app code changed; lint stayed at 0 errors / 19 warnings. Now on `^10.6.0`. | #132 |

Several optional improvements shipped alongside the triage:

| Improvement | What landed | PR |
| --- | --- | --- |
| API dependency pinning. | Pinned `starlette`/`fastapi` (and bounded the version range) so the resolved API runtime stops drifting silently between builds. | #100 |
| Nonce-based script CSP at the web edge. | Added a per-request nonce CSP (`script-src 'self' 'nonce-<n>' 'strict-dynamic'`, `style-src 'self' 'unsafe-inline'`, plus base-uri/object-src/frame-ancestors/form-action), making this the single authoritative CSP. This is the "deliberately deferred" nonce CSP the second pass flagged. Documented relaxations: `style-src 'unsafe-inline'` (pervasive inline `style={{}}` can't be nonce/hash-covered) and no default/connect/img restriction (preserves the cross-origin Voice Live `wss://` and `data:`/`blob:` backgrounds). | #101 |
| Next 16 `middleware`→`proxy` migration. | Renamed `app/web/src/middleware.ts` → `src/proxy.ts` and export `middleware`→`proxy` (Next 16 convention; logic byte-identical). Eliminates the deprecation warning and prevents Next 16/17 from silently dropping the CSP. | #102 |
| Web manifest hygiene. | Widened `engines.node` `">=22.0.0 <23"` → `">=22.0.0 <27"` so the manifest stops lying about the `node:26-alpine` image (lower bound still matches CI's Node 22; upper bound `<27` deliberately trips on a future Node 27 so the constraint stays honest), and fixed the stale `src/middleware.ts` → `src/proxy.ts` doc-comments left by #102. The coupled `eslint-config-next` → `^16` bump was attempted in the same pass and reverted (see open items). | #106 |

## Known open items

Both items originally tracked here are now resolved. Kept for the paper trail
instead of being silently deleted.

**Fixed since this audit:** no PR CI job used to actually `docker build` either
Dockerfile — the base-image bumps (#90/#91) and the original `engines.node`
mismatch all slipped past PR CI because the Dockerfiles were only built in
`deploy.yml` on push-to-main. `.github/workflows/docker-build.yml` (#187) now
builds `app/web/Dockerfile` and `app/api/Dockerfile` (build-only, `push: false`)
on every PR that touches `app/web/**` / `app/api/**`, with GHA layer caching and
an API import smoke test. `proxy/Dockerfile` stays excluded by design — it's
vendored SimpleL7Proxy (the gateway path) and is already covered by `hadolint`
and the `proxy-dotnet` build/test job.

**Resolved since this audit:** the `eslint-config-next` `^16` / `eslint` 9→10 knot (formerly listed here) is fixed — #124 reworked `eslint.config.mjs` onto config-next 16's native flat-config export (dropping the `@eslint/eslintrc` `FlatCompat` bridge), and #132 completed the eslint 9→10 bump. `eslint-config-next` is now `16.2.10` and `eslint` is `^10.6.0`. Note: `npm ci` still prints benign `ERESOLVE overriding peer dependency` warnings for `eslint-config-next`'s bundled `eslint-plugin-import`/`eslint-plugin-jsx-a11y`/`eslint-plugin-react` — their published peer ranges still cap at `eslint@^9`/`^9.7` as of this writing (verified via `npm view <pkg> peerDependencies`, no newer release exists). Install still exits 0, everything dedupes to the single `eslint@10.6.0`, and lint/build/test are unaffected (0 errors, matching the pre-bump warning baseline). Not a regression and nothing to override locally — there is no published version of those plugins yet that declares an `eslint@10` peer.

**Fixed since this audit:** the CI-vs-image runtime skew this doc's own table
(rows for #91/#90 above) left in place is resolved. `app/api/Dockerfile`
(`python:3.14-slim`) and `app/web/Dockerfile` (`node:26-alpine`) both drifted
past what `app-ci.yml` actually tests (Python 3.12, Node 22), and — for the same
no-PR-docker-build reason described in the first item above — nothing in PR CI
caught it. The Python bump surfaced real `azure-cosmos`/`aiohttp` `DeprecationWarning` noise in
production logs; the Node bump had no equivalent functional symptom, but
`npm view next typescript eslint vitest engines` confirms none of them require
Node 26, and #106's `engines.node` widening to `<27` was manifest hygiene ("stop
contradicting the image") rather than a documented technical need. Both
Dockerfiles are reverted to the CI-tested majors (#187): `python:3.12-slim` and
`node:22-alpine`, with `package.json`'s `engines.node` narrowed back to
`">=22.0.0 <23"`. Each Dockerfile now carries a comment tying its pinned version
to `app-ci.yml`/AGENTS.md so a future Dependabot major bump has to be a
deliberate, documented decision instead of a silent merge.

## Bottom line

The original audit backlog is cleared, and the Dependabot backlog is fully worked
off — the last genuinely-blocked item (#92) shipped via #124 (native flat-config
rework) and #132 (eslint 10). The two quick web-manifest fixes — `engines.node`
honesty and the stale proxy doc-comments — shipped in #106. The PR-CI Docker-build
gap is now closed too (#187, see "Known open items"), and so is the CI-vs-image
runtime skew it had been masking — both Dockerfiles are back on the CI-tested
majors (#187, see "Known open items"). What remains is the one
accepted cost tradeoff. That is a healthy steady state: explicit debt with owners
and reasons, not silent rot.
