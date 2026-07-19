# AI4IA Platform Audit

> **Audit status (2026-07-19):** repository and PR review refreshed after all
> accepted audit work merged to `main` through
> `815bb6ad78a6b117dd754bf315b4a055f295c1e2`. This report distinguishes
> repository truth from runtime evidence; it does **not** claim that the merged
> revisions are deployed.
>
> **Governance risk:** `main` currently has **no branch protection and no repository
> ruleset**. A direct push can bypass every CI workflow and review. The owner should
> first add always-emitted aggregate checks that run on every PR and report explicit
> no-op success when path-scoped work does not apply. Only those unconditional
> aggregates—not conditional job names—should become required checks, alongside a
> pull request, one approving review, stale-approval dismissal, conversation
> resolution, and force-push/deletion protection. This audit deliberately did not
> change live repository settings.

## Scope and method

The audit covered:

- architecture, user/operator, feature, component, and historical audit documents;
- portal catalog source/generation and Markdown completeness;
- Bicep wiring for FastAPI, SimpleL7Proxy, APIM products/APIs/subscriptions, Foundry
  RBAC, canonical data, and feature prerequisites;
- API/web sources for routing, ownership, agent execution, redacted activity,
  Voice Live/TTS, WebIQ, Foundry Toolbox, error behavior, and observability;
- current heads, descriptions, changed files, diffs, and merge status for PRs
  #184-#192;
- contributor guidance and CI workflow definitions; and
- GitHub REST evidence for branch protection/rulesets.

Evidence was prioritized in this order: executable configuration and tests, runtime
source, generated catalogs, current PR heads, then prose. Contradictory prose was
corrected rather than treated as architecture evidence. No Azure resource mutation,
deployment, live canary, APIM compiler run, or production what-if was performed.

## Confirmed architecture

The durable system description is in [`architecture.md`](./architecture.md).
The audit confirmed these load-bearing facts:

| Concern | Confirmed design |
| --- | --- |
| Compatible model traffic | FastAPI -> SimpleL7Proxy -> model APIM -> catalog-selected Foundry deployment |
| Realtime/Voice Live | Browser -> FastAPI relay -> realtime APIM -> Foundry; SimpleL7Proxy is bypassed only because it does not support WebSockets |
| Core gateway credentials | Separate proxy-ingress, proxy-to-model-APIM, and realtime-APIM credentials; the proxy strips the first and alone holds/injects the second |
| Optional Speech Voice Live | Default-off second provider with a fourth distinct APIM subscription and scoped managed-identity backend path |
| Canonical state | Cosmos for user-scoped sessions/messages/usage/agents/workflows/MCP records/document manifests; Blob for source documents and artifacts |
| Derived state | Memory vectors, document chunks, parsed artifacts, and search indexes are rebuildable |
| Enforcement | FastAPI owns auth, user normalization, ownership, feature gates, entitlements, tools, persistence, and metering |
| Tool safety | Registration and execution checks, approvals, bounded calls, public-HTTPS/SSRF validation, scoped secrets, and structured failures |
| Activity visibility | Structured step kind, validated tool alias/name, fixed reason category, and coarse outcome only; never hidden reasoning, arguments/results, credentials, prompts/queries, URLs, remote exception text, audio, or transcripts |
| Browser error telemetry | Content-free event/code/severity/boolean schema with client deduplication and per-user server throttling; no message, route, component, URL, stack, token, or remote error text |
| Native Azure paths | Content Understanding, Monitor, Key Vault, Storage, Cosmos, and AI Search use their native data/control planes rather than pretending to be model traffic |

## Documentation findings and remediation

| Finding | Resolution in this workstream |
| --- | --- |
| `architecture.md` mixed current and migration prose, repeated policy details, and incorrectly said one key covered both proxy ingress and realtime | Rewritten around components, boundaries, request paths, state, agents/tools, activity, failures, observability, controls, tradeoffs, and residual gaps; all core credentials are mapped separately |
| Deployment diagrams conflated model, realtime, and MCP APIs and omitted trust semantics | Replaced with maintainable Mermaid plus editable Excalidraw and rendered SVG system/data-flow views |
| Activity and telemetry prose overstated current privacy | #189 removed argument-derived activity details and parsed-argument INFO logs; architecture and user docs now describe the merged metadata-only contract |
| Historical audit files were easy to mistake for current posture | Kept them as provenance and added this evergreen, status-labeled audit report as the current index |
| Foundry Toolbox docs blurred supported tool types with the three tools in the canonical toolbox | Added an explicit available-versus-deployed distinction |
| Toolbox `--create` repeat behavior was under-specified | Documented safe version creation and warned that repeat calls are not a no-op |
| WebIQ browse behavior under active remediation could be read as current | #184 merged its bounded live-crawl/pending-retry behavior; docs distinguish WebIQ from Foundry Toolbox web search |
| Voice docs emphasized Voice Live but not turn-based TTS routing | Clarified that TTS/transcription are HTTP calls on the normal proxy path and are independent of the Voice Live socket |
| Proxy README repeated the stale shared-ingress/realtime-key claim | #185 corrected it to the three-credential core design; this rebase preserves that merged wording |
| Docs portal would not surface an evergreen audit | Added this report to `site/data/docs.manifest.json` and regenerated `docs.js` |

`AGENTS.md` states the routing, catalog, ownership, feature-gate, Cosmos,
execution-time tool, and secret invariants. This workstream preserves those
invariants and corrects its Python image-history note: runtime skew was proven, but
the audit did not establish that Python 3.14 caused the reported dependency
warnings.

## Parallel implementation work

All accepted implementation work is now on `main`. The table records squash-merge
commits, not deployment revisions.

| PR | Main commit | Category | Merged outcome |
| --- | --- | --- | --- |
| #184 | `e08afa0` | Foundry Toolbox and WebIQ | Bounded WebIQ crawl/retry behavior, fail-closed official MCP posture, and Toolbox SDK/schema/provisioning coverage |
| #185 | `18fd952` | Gateway/proxy and API health | Confirmed routing invariant, separated credential scopes, constant-time proxy auth comparison, and health probes |
| #186 | Closed | Chat rendering proposal | Superseded by the narrower persistence-ordering fix in #192 |
| #187 | `fd22072` | CI/Docker | PR-time API/web image builds, Docker context hardening, runtime-version alignment, and operator-script tests |
| #188 | `13f2dd6` | Voice lifecycle and TTS | Capture/playback recovery, atomic session finalization, navigation escape, visible provider guidance, and content-free media-failure telemetry |
| #189 | `55d4278` | API reliability and privacy | Ownership/concurrency fixes, safe session normalization, aggregation-aware metrics, fail-closed mem0 erase, deterministic MCP aliases, metadata-only activity/logs, and explicit failures |
| #190 | `5c01b1e` | Web UX and help | Accessible advanced-setting/tool help, model grouping, modal/focus consistency, and content-free client-event telemetry |
| #191 | `815bb6a` | Documentation and architecture | Published the evergreen audit, corrected architecture and operator guidance, and added synchronized editable/rendered system diagrams |
| #192 | `299b520` | Chat rendering reliability | Persists terminal assistant state before stream completion and reconciles fallback messages without duplicate writes |

The published audit snapshot incorporated each implementation merge before publication.
A merged repository change is still not evidence of deployment.

## Benefits

- Operators get one source for request paths, credentials, ownership, failure
  semantics, and residual risk.
- Developers can distinguish the HTTP/SSE gateway invariant from the intentional
  WebSocket exception without opening Bicep.
- Security review can trace each credential to one holder, hop, and API/product
  scope.
- Agent activity remains useful without exposing chain-of-thought or user/tool
  argument content.
- Historical findings remain available without being mistaken for current
  implementation status.
- The portal completeness gate keeps this report and architecture sources
  discoverable.

## Validation matrix

Run from the repository root unless a command says otherwise:

```powershell
# Documentation portal completeness and generated-file drift
python scripts/gen-docs-catalog.py --check

# Catalog and gateway contracts referenced by the architecture
python scripts/gen-model-catalog.py --check
python scripts/gen-mcp-catalog.py --check
python scripts/gen-voice-provider-catalog.py --check
python scripts/gen-gateway-policy.py --check
python scripts/validate-catalog.py
python scripts/validate-feature-prereqs.py

# Focused policy/catalog tests
python -m unittest scripts.tests.test_gateway_policy
python -m unittest scripts.tests.test_voice_provider_catalog

# JSON/source and diagram checks used by this workstream
Get-Content docs/architecture-overview.excalidraw -Raw | ConvertFrom-Json | Out-Null
Get-Content site/data/docs.manifest.json -Raw | ConvertFrom-Json | Out-Null
```

The documentation workstream additionally checks Markdown relative links and image
paths, balanced Mermaid fences, transparent Excalidraw containers, filled leaves,
black text with explicit dimensions, unique element ids, and the rendered SVG
`viewBox`/content labels.

Implementation PR final heads passed their full GitHub checks before merge,
including web lint/test/build, API ruff/pyright/pytest, Bicep/schema validation,
proxy .NET build/tests, quality, CodeQL, security scans, and PR-time Docker image
builds. The documentation workstream reran applicable drift, link, schema, and
diagram checks after every main integration; the resulting `main` revision passed
all post-merge workflows.

## Residual gaps

| Gap | Impact | Current mitigation |
| --- | --- | --- |
| No `main` ruleset or branch protection | Reviews and checks can be bypassed | Owner action required; audit did not mutate live settings |
| `app-ci`, `infra-validate`, and `docker-build` are path-filtered | Requiring their conditional job contexts directly would deadlock docs-only and other out-of-scope PRs because those contexts are never emitted | Add always-emitted aggregate/no-op-success contexts first, then require only those unconditional aggregates |
| No evidence in this audit of production deployment parity | Repository truth may lead the live revision | Use inventory, revision SHA, smoke tests, and approved what-if before claiming parity |
| Proxy queue state is per-replica memory | No durable/global ordering or exact fairness | Warm replica, bounded expiry/requeue, explicit telemetry limitations |
| Basic v2 APIM is single-region/capacity-one by current design | Gateway outage/capacity concentration | Monitor and make an explicit cost/reliability scaling decision |
| Speech Voice Live production gate is open | Optional provider is not proven end-to-end live | Keep default-off until policy compiler, RBAC/audience, what-if, canary, and manual tests pass |
| Tool/browser/AI preview surfaces | Contract and availability can change | Feature gates, curated catalogs, bounded output, and deliberate provisioning |
| Some telemetry dimensions are unavailable | Operators cannot infer exact queue/provider state | Panels report partial/stale/unavailable instead of fabricated values |
| mem0 cannot prove hard deletion with the pinned SDK | An erase request cannot satisfy a hard-delete promise | `supportsDelete=false`; destructive calls fail closed; `/forget` states that no records were deleted |
| Memory consent and recalled-memory indicator are absent | Users lack a global control and provenance cue | Keep owner-scoped management; design explicit controls before expansion |

## Owner actions

| Priority | Owner action | Exit evidence |
| --- | --- | --- |
| P0 | Add a `main` ruleset requiring pull requests, one approving review, stale-approval dismissal, conversation resolution, and blocked force-push/deletion | Ruleset visible through GitHub API/UI and a test PR cannot merge without requirements |
| P0 | Add always-emitted aggregate checks for application, infrastructure, container, quality, and security validation; each aggregate must run on every PR and report explicit no-op success when its path-scoped jobs do not apply. The container aggregate must include web image, API image, and `dockerignore context boundary` results | Docs-only and representative scoped PRs all emit the same aggregate contexts; applicable child failures make the aggregate fail |
| P0 | Require only those unconditional aggregate contexts after a dry run proves they are always emitted; do not directly require conditional `app-ci`, `infra-validate`, or `docker-build` job names | Ruleset lists only unconditional aggregate contexts and both docs-only and scoped test PRs remain mergeable when green |
| P1 | Record the deployed API/web/proxy revision SHAs and run approved post-deploy smoke tests | Revision inventory and timestamped smoke result |
| P1 | Decide whether Docker Dependabot updates should be limited to minor/patch or instead require explicit owner review for major runtime changes | Policy prevents another unreviewed runtime-major drift while preserving deliberate upgrades |
| P1 | Keep Speech Voice Live disabled until all runbook gates close | Approved compiler/what-if/RBAC/audience/canary/manual evidence |
| P2 | Decide whether Basic v2 capacity/region posture meets the target SLO and budget | Written SLO, capacity decision, and alert thresholds |
| P2 | Define memory consent/provenance UX and stable proxy/provider telemetry dimensions | Accepted design and tracked implementation issue/PR |
