# AI4IA Platform Audit

> **Audit status (2026-07-18):** repository and PR review refreshed after #185 and
> #187 merged to `main`. This report separates merged source, open proposals, and
> deployment status; it does **not** claim that any repository change is deployed.
>
> **Acceptance dependency:** PR #191 must not merge until PR #189 contains and
> merges privacy-hardening commit `c351131`, which removes
> prompt/query/tool-argument content from user-facing activity and ordinary agent
> INFO logs. The fix exists on the reviewed #189 head
> `c351131c7802ba6041011067e94c48a09ed864e7` but is not on `main` or deployed.
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
  #184-#190;
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
| Activity visibility target | Structured step kind, tool name, and coarse outcome only; never hidden reasoning, arguments/results, credentials, prompts/queries, audio, or transcripts |
| Current activity/privacy gap | Current `main` allowlists argument text including `prompt` and logs redacted parsed arguments at INFO; open PR #189 commit `c351131` removes both, and PR #191 depends on that commit merging |
| Native Azure paths | Content Understanding, Monitor, Key Vault, Storage, Cosmos, and AI Search use their native data/control planes rather than pretending to be model traffic |

## Documentation findings and remediation

| Finding | Resolution in this workstream |
| --- | --- |
| `architecture.md` mixed current and migration prose, repeated policy details, and incorrectly said one key covered both proxy ingress and realtime | Rewritten around components, boundaries, request paths, state, agents/tools, activity, failures, observability, controls, tradeoffs, and residual gaps; all core credentials are mapped separately |
| Deployment diagrams conflated model, realtime, and MCP APIs and omitted trust semantics | Replaced with maintainable Mermaid plus editable Excalidraw and rendered SVG system/data-flow views |
| Activity and telemetry prose overstated current privacy | Documented the strict metadata-only target, the current prompt/query argument leak, and the blocking dependency on the #189 privacy fix |
| Historical audit files were easy to mistake for current posture | Kept them as provenance and added this evergreen, status-labeled audit report as the current index |
| Foundry Toolbox docs blurred supported tool types with the three tools in the canonical toolbox | Added an explicit available-versus-deployed distinction |
| Toolbox `--create` repeat behavior was under-specified | Documented safe version creation and warned that repeat calls are not a no-op |
| WebIQ browse behavior under active remediation could be read as current | This report labels PR #184 open; current docs do not claim its pending-crawl/retry fixes are merged |
| Voice docs emphasized Voice Live but not turn-based TTS routing | Clarified that TTS/transcription are HTTP calls on the normal proxy path and are independent of the Voice Live socket |
| Proxy README repeated the stale shared-ingress/realtime-key claim | #185 corrected it to the three-credential core design; this rebase preserves that merged wording |
| Docs portal would not surface an evergreen audit | Added this report to `site/data/docs.manifest.json` and regenerated `docs.js` |

`AGENTS.md` already states the correct routing, catalog, ownership, feature-gate,
Cosmos, execution-time tool, and secret invariants. This workstream does not change
those invariants or the documented CI commands, so it intentionally leaves
`AGENTS.md` unchanged.

## Parallel implementation work

PRs #185 and #187 are merged to `main`; the remaining entries are open. A merged
repository change is still not evidence of deployment.

| PR | Reviewed head | Category | Proposed/remediated work | Status used by this report |
| --- | --- | --- | --- | --- |
| #184 | `8744ee23c73f` | Foundry Toolbox and WebIQ | WebIQ browse live-crawl/pending-retry handling, official MCP fail-closed catalog checks, Toolbox example/provisioning/doc corrections | Open; do not claim merged/deployed |
| #185 | merge `18fd952e91d9` | Gateway/proxy and API health | Confirms routing invariant, separates credential wording, constant-time proxy auth comparison, and API health probes | Merged to `main`; deployment not established |
| #186 | `b0a8dc033750` | Chat rendering reliability | Preserves streamed assistant content when post-stream reconciliation fails | Open; not claimed as current failure behavior |
| #187 | merge `fd22072e29c9` | CI/Docker | PR-time API/web image builds plus Docker/CI hygiene | Merged to `main`; Docker jobs exist but are not protected required checks |
| #188 | `76038f945600` | Voice lifecycle and TTS | Input/playback lifecycle, stop/retry/session locking, and TTS behavior fixes | Open; user docs describe intent without claiming these fixes shipped |
| #189 | `c351131c7802` | API reliability and privacy | Ownership, race, persistence, metrics, explicit failures, and metadata-only activity/log privacy hardening | Open; required privacy commit exists but is unmerged, so PR #191 remains blocked on #189 merging |
| #190 | `ea67ecd1693b` | Web UX and help | Accessibility, advanced-setting/tool help, client telemetry, modal/focus, and UI consistency | Open; tooltip/help improvements are not described as current `main` |

Because these branches can evolve, merge owners should refresh this table and rerun
the validation matrix after each PR merges. Resolve overlapping documentation
changes from #184 against the evergreen architecture and audit rather than
restoring point-in-time wording. The #191 rebase retains #185's merged topology,
credential, proxy-auth, and health-probe facts and #187's merged CI posture.

## Benefits

- Operators get one source for request paths, credentials, ownership, failure
  semantics, and residual risk.
- Developers can distinguish the HTTP/SSE gateway invariant from the intentional
  WebSocket exception without opening Bicep.
- Security review can trace each credential to one holder, hop, and API/product
  scope.
- Once the required #189 privacy fix lands, agent activity remains useful without
  exposing chain-of-thought or user/tool argument content.
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

Full implementation validation remains owned by the remaining PRs and existing CI:
web lint/test/build, API ruff/pyright/pytest, Bicep/schema validation, proxy .NET
build/tests, quality, CodeQL, security scans, and PR-time Docker image builds.

## Residual gaps

| Gap | Impact | Current mitigation |
| --- | --- | --- |
| No `main` ruleset or branch protection | Reviews and checks can be bypassed | Owner action required; audit did not mutate live settings |
| `app-ci`, `infra-validate`, and `docker-build` are path-filtered | Requiring their conditional job contexts directly would deadlock docs-only and other out-of-scope PRs because those contexts are never emitted | Add always-emitted aggregate/no-op-success contexts first, then require only those unconditional aggregates |
| Open PRs #184, #186, and #188-#190 | Proposed fixes are not guaranteed on `main` or deployed | Status is explicit; merge and validate independently |
| Activity and INFO logs can contain ordinary prompt/query argument text | User content can reach persisted activity or telemetry despite secret redaction | Block PR #191 on the corresponding #189 privacy-hardening commit; restrict activity/log access until it is deployed |
| No evidence in this audit of production deployment parity | Repository truth may lead the live revision | Use inventory, revision SHA, smoke tests, and approved what-if before claiming parity |
| Proxy queue state is per-replica memory | No durable/global ordering or exact fairness | Warm replica, bounded expiry/requeue, explicit telemetry limitations |
| Basic v2 APIM is single-region/capacity-one by current design | Gateway outage/capacity concentration | Monitor and make an explicit cost/reliability scaling decision |
| Speech Voice Live production gate is open | Optional provider is not proven end-to-end live | Keep default-off until policy compiler, RBAC/audience, what-if, canary, and manual tests pass |
| Tool/browser/AI preview surfaces | Contract and availability can change | Feature gates, curated catalogs, bounded output, and deliberate provisioning |
| Some telemetry dimensions are unavailable | Operators cannot infer exact queue/provider state | Panels report partial/stale/unavailable instead of fabricated values |
| Memory consent and recalled-memory indicator are absent | Users lack a global control and provenance cue | Keep owner-scoped management; design explicit controls before expansion |

## Owner actions

| Priority | Owner action | Exit evidence |
| --- | --- | --- |
| P0 | Add a `main` ruleset requiring pull requests, one approving review, stale-approval dismissal, conversation resolution, and blocked force-push/deletion | Ruleset visible through GitHub API/UI and a test PR cannot merge without requirements |
| P0 | Add always-emitted aggregate checks for application, infrastructure, container, quality, and security validation; each aggregate must run on every PR and report explicit no-op success when its path-scoped jobs do not apply. The container aggregate must include web image, API image, and `dockerignore context boundary` results | Docs-only and representative scoped PRs all emit the same aggregate contexts; applicable child failures make the aggregate fail |
| P0 | Require only those unconditional aggregate contexts after a dry run proves they are always emitted; do not directly require conditional `app-ci`, `infra-validate`, or `docker-build` job names | Ruleset lists only unconditional aggregate contexts and both docs-only and scoped test PRs remain mergeable when green |
| P0 | Merge PR #189 privacy-hardening commit `c351131` before accepting PR #191 | Tests prove activity and INFO telemetry contain metadata only; #189 merged SHA is recorded here |
| P0 | Merge the remaining implementation PRs only after each branch is rebased, conflicts are resolved, and its stated validation is rerun | Merged SHA plus green required checks; report table updated |
| P1 | Reconcile #184 documentation hunks against this architecture/audit instead of reintroducing stale wording | Final merged docs preserve the credential, privacy-dependency, and status tables |
| P1 | Record the deployed API/web/proxy revision SHAs and run approved post-deploy smoke tests | Revision inventory and timestamped smoke result |
| P1 | Keep Speech Voice Live disabled until all runbook gates close | Approved compiler/what-if/RBAC/audience/canary/manual evidence |
| P2 | Decide whether Basic v2 capacity/region posture meets the target SLO and budget | Written SLO, capacity decision, and alert thresholds |
| P2 | Define memory consent/provenance UX and stable proxy/provider telemetry dimensions | Accepted design and tracked implementation issue/PR |
