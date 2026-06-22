# Brutal Repo Audit

This repo is ambitious, useful, and dangerously tolerant of operator pain. The
architecture is not the embarrassing part; the embarrassing part is how many
ways the setup can look green while the deployment is stale, mis-owned,
half-configured, or expensive.

## Verdict

AI4IA is a serious Azure AI workload wearing a demo repo hoodie. It has real
governance ideas: APIM/SimpleL7Proxy in front of model calls, managed identity,
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
| Web builds | Docker and CI could fall back from `npm ci` to `npm install`, destroying reproducibility. | Docker and CI now use deterministic `npm ci`; Docker uses Node 22 to match CI. |
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

The audit backlog is cleared. The only open items are the accepted cost tradeoffs above.
