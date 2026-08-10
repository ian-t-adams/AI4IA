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
| **P1 Security decision** | **The main API identity can bypass the model gateway if compromised.** `nativeFoundryPrincipalIds` contains the API UAMI, so every regional Foundry account grants it `Cognitive Services OpenAI User`. That resource-scoped role permits direct inference with any deployed model; there is no documented deployment-only role. The running code reserves direct Foundry use for Responses Code Interpreter, but IAM does not enforce that intent. | With explicit owner approval, introduce a dedicated identity/workload for Responses Code Interpreter, scope it only to the required account, migrate the direct exception, then remove broad Foundry inference from the main API UAMI. Validate startup, files/responses calls, metering, and gateway paths before removing the old grant. Do not weaken APIM or add a second general model route. | `infra/main.bicep` (`nativeFoundryPrincipalIds`), `infra/modules/foundry.bicep`, [`architecture.md`](./architecture.md#pending-decision-isolate-direct-code-interpreter-authority) |
| **P1 Security design** | **Network isolation is partial scaffolding, not a served private/regulated mode.** Direct Bicep parameters `vnetIsolationEnabled` / `dataTierPrivate` are absent from `main.parameters.json` and normal azd/CI mapping. Existing private endpoints omit ACR, App Configuration, Search, Foundry, APIM, and monitoring, so enabling the partial graph cannot establish an end-to-end private boundary. | Define the complete control/data-plane endpoint and private-DNS matrix; include an off-VNet-safe deployment/operations path; wire server-authoritative azd/CI inputs only after prerequisites are explicit; then prove a cold deploy and capability acceptance from inside the isolated network. Until then, do not advertise or enable these flags as sufficient isolation. | `infra/main.bicep`, `infra/modules/network.bicep`, `infra/modules/privateendpoints.bicep`, [`architecture.md`](./architecture.md#tradeoffs-and-residual-gaps) |
| **P1** | **A second app can now claim its own APIM credentials, but cannot yet onboard across subscriptions.** The naming collision is fixed: every APIM **child entity** name is derived from `${workload}` (`apimcore.bicep`, `gateway.bicep`, `mcpgateway.bicep`), `workload` has an `${AI4IA_WORKLOAD=ai4ia}` override, `deploy.yml` derives `rg-${AI4IA_WORKLOAD:-ai4ia}-${AZURE_ENV_NAME}`, and `ApimChildNamingTests` in `test_bicep_naming.py` fails any child name that reverts to a literal. The default resolves byte-identically to today's names, so no live subscription key rotated. What remains is genuine platform work: `apimcore.bicep` **creates** the APIM service, so a second app in another subscription has no way to reference the existing one; the `openai` and `realtime` **APIs** are deliberately shared rather than workload-derived (a second app reuses them, and the guard excludes them on purpose); and nothing registers a second app in API Center. | Split the shared plane from the consumer: a module that takes an **existing** APIM by resource id, and per-app product/subscription/policy attached to it. Adopt **APIM workspaces** at that point rather than a second instance — they already work on Basic v2, so federation does not by itself force the Standard v2 upgrade (private backends do). | `infra/modules/apimcore.bicep`, `gateway.bicep`, `mcpgateway.bicep`, [`architecture.md`](./architecture.md) |
| **P2** | **Durable execution now covers workflows but not the other background work.** `POST /api/workflows/{name}/run` accepts an opt-in `"durable": true` that runs the workflow on an Azure Durable Task Scheduler orchestration (`workflows/durable.py`). The flag is **enabled in production as of 2026-08-02**, so a scheduler is provisioned and a workflow no longer has to die with the replica. Chat and document enrichment still use in-process tasks. Enrichment now has process-wide admission/concurrency bounds and reloads raw bytes from canonical Blob only after admission, while chat streaming has bounded producer backpressure; those controls bound replica memory but do not make either task durable across deploy or scale-in. | Move document cracking onto **Container Apps Jobs** (same image, identity, and networking; no new SDK). **Not Durable Functions** — it couples to Functions compute, adding a second platform, pipeline, and RBAC surface for no gain here. Any new compute must still route model calls proxy → APIM → Foundry per AGENTS.md rule 1. | `workflows/durable.py`, `routers/chat.py`, `library/ingest.py`, [`feature-enablement.md`](./runbooks/feature-enablement.md) |
| **P2** | **Session deletion can leave late-arriving child orphans.** Messages and uploaded session documents use `/sessionId` partitions in separate containers from the `/userId`-partitioned session. A writer that passed ownership immediately before a delete can still land after the cascade. A request-time sweep cannot fence another replica, while a lightweight point reconciliation is impossible without first running a cross-partition child scan. | Add an age-gated change-feed or scheduled reconciliation job that scans child containers and point-checks parent existence, or redesign the write/delete transaction boundary. Do not reintroduce timing-based tombstone loops that still miss cross-replica writes. | `sessions/cosmos_repo.py::delete_session` |
| **P2** | **Gateway is single-region / capacity-1** (APIM Basic v2) and the SimpleL7Proxy queue is per-replica memory (no global ordering/fairness). | Decide whether the capacity/region posture meets the target SLO and budget; set scaling + alert thresholds if not. | [`architecture.md`](./architecture.md) |
| **P1** | **Rollback cannot undo an infrastructure regression.** `post-deploy-verify.py rollback` restores container *revisions*; nothing reverts what `azd provision` changed (APIM policy and fragments, named values, model deployments, RBAC). A bad generated gateway policy therefore survives rollback and re-fails every subsequent deploy. Demonstrated on 2026-08-05: a duplicate backend label took the whole model plane down and seven consecutive deploys rolled back without touching the cause. **Partially mitigated**: `scripts/tests/test_policy_json_shape.py` now fails CI on a duplicate `JObject` property in any committed policy, which is the specific class that caused that outage. The general gap stands — any *other* runtime-only policy failure still ships and still cannot be rolled back. | Either capture and restore the prior APIM policy/fragment revisions alongside the container revisions, or gate policy changes behind a pre-deploy validation that executes a request against the compiled policy rather than only compiling it. Compilation is not enough — the failing expression was valid C#. | `scripts/post-deploy-verify.py`, [`deployment.md`](./runbooks/deployment.md) |
| **P2** | **Memory UX gaps.** No global memory-consent control and no recalled-memory provenance indicator; management is owner-scoped CRUD only. | Design explicit consent + provenance UX before expanding memory surfaces. | [`memory.md`](./memory.md) |
| **Ops** | **Both Voice Live providers are enabled and authenticated-canary-proven; manual audio acceptance remains.** The current repository and API settings enable `azure_openai,speech_voice_live`, Azure OpenAI remains the default, the Speech APIM API exists, and direct-FastAPI canaries for both providers returned `outcome=success` on 2026-08-08. The Bicep template still defaults Speech off. | Keep canaries pointed at the `wss://` form of `AZURE_API_URL`, never the web/Next.js hostname. Complete the signed-in microphone/provider-switch/transcript regression after relevant releases; it validates audio and browser UX beyond the protocol canary. | [`deployment.md` Voice posture](./runbooks/deployment.md#speech-voice-live-posture-enabled-in-the-current-environment), [`feature-enablement.md`](./runbooks/feature-enablement.md#speech-voice-live-second-voice-provider) |
| **P2** | **The app still has large orchestration components.** `ChatApp.tsx` (~3.0K lines), `routers/realtime.py` (~1.9K), `ConversationInspector.tsx` (~1.6K), and `routers/chat.py` (~2.0K) are functional and well-tested, but expensive to review and easy for sibling changes to touch simultaneously. `chat.py` is down from ~2.5K since its streaming lifecycle moved to `routers/_chat_streaming.py`, which is the pattern the rest should follow. | Extract state machines/hooks/services by responsibility (session navigation, stream lifecycle, approval lifecycle, voice lifecycle, inspector drafts) while preserving the mutation-tested seams. Do this incrementally; a rewrite would discard the hard-won race coverage. | Source modules + `AGENTS.md` sibling-merge rule |
| **P2** | **Repository → live parity is still a release concern, not automatically true.** Exact-digest promotion and canary verification are shipped, but infra/APIM/RBAC changes are incremental and container rollback cannot restore them. | Keep live assertions for forbidden stale roles/resources, add APIM policy/version rollback, and consider deployment stacks only for narrowly allowlisted resource groups. | [`deployment.md`](./runbooks/deployment.md), `post-deploy-verify.py` |

> **Document understanding is now live-verified.** On 2026-08-07 the first real
> upload exposed that the account had no Content Understanding model defaults.
> The resource now maps the analyzer's logical completion names to its supported
> `gpt-5.2` deployment and its embedding name to `text-embedding-3-large`;
> direct CU analysis and the app upload path both returned **Succeeded / ready**
> with grounded Markdown and a Summary field. The postprovision hook now makes
> those data-plane defaults durable, is executed by regression tests, and the
> production deploy + canary completed successfully (#320, #321). The failed
> deployment that exposed the first hook bug is part of the record, not hidden.

## Owner decisions from the 2026-08-03 audit

> **Considered and rejected: passing the signed-in user's token through to
> Foundry.** It is the natural intuition for P1-4 and it makes the problem
> materially worse. Three independent reasons: (1) the user's token is issued for
> *this API's* audience, not `https://cognitiveservices.azure.com`, so it would
> need an on-behalf-of exchange before Foundry would look at it; (2) Foundry
> data-plane access is RBAC on the calling identity, so **every user** would need
> `Cognitive Services OpenAI User` on the account — a per-user role assignment,
> and the opposite of least privilege; (3) decisively, a user holding a Foundry
> token can call Foundry *directly from anywhere*, which bypasses APIM entirely —
> no rate limiting, no residency policy, no usage metering, no priority routing.
> P1-4 is about making gateway-only routing an IAM boundary; user passthrough
> removes the boundary for everyone instead of tightening it for one identity. It
> also would not fix the stated problem, since `id-api` keeps its roles regardless.
>
> The identity model is deliberate: the *user* is authenticated at the API edge,
> and the *platform* is authorized to Foundry. Those are different questions.

Five items the audit raised that were deliberately **not** actioned, because each
needs a judgement only the owner can make — RBAC changes, a provision run, or a
product tradeoff. Each row states the concrete lever and what it costs, so the
decision does not have to be re-derived. Full context in the
[repository audit](./repository-audit-2026-08-03.md).

| Item | The lever | What it costs | Why it wasn't done |
| --- | --- | --- | --- |
| **P1-4 — gateway-only routing is convention, not IAM.** **Key and wildcard bypasses closed; one explicit direct-inference exception remains.** `disableLocalAuth=true`; `id-api` holds only narrow Content Understanding access plus `Cognitive Services OpenAI User` for the Responses-API Code Interpreter. | The original “move CI then drop OpenAI User” plan was wrong while `id-api` also held `Cognitive Services User` (`Microsoft.CognitiveServices/*`); that wildcard was the real bypass and is gone. A separate CI workload would now remove the last direct OpenAI role, but it adds a Container App/identity/deploy surface for a documented, metered, entitlement-gated exception. | Residuals: Code Interpreter remains direct by design; APIM still holds the broad Cognitive Services User role because it fronts OpenAI + Speech Voice Live. Narrowing APIM should follow the manual Voice Live proof, not precede it. | `scripts/tests/test_foundry_role_scope.py`, [`architecture.md`](./architecture.md) |
| **P1-7 — the tested artifact is not the *PR-tested* artifact.** *Partly closed by #296:* both app base images are digest-pinned and `deploy.yml` builds each service once and deploys `--from-package <ref>@sha256:<digest>`, so nothing rebuilds inside a deploy and the running revision traces back to a commit. What remains is that the image is built by the **deploy** workflow, not promoted from the PR that tested it — plus no SBOM, signature, provenance, or blocking image scan, and `proxy/Dockerfile`'s MCR bases stay on moving tags because CI does not build that image. | Publish the PR-built image to a staging repository (or GHCR) under a PR-scoped identity, then re-tag it by digest into the production ACR after merge. Separately: add SBOM/signing/provenance and a blocking scan to the build step. | Letting PR code push to a registry the production apps pull from is a **security-posture change**: a malicious or merely broken PR could publish there, and the OIDC identity would need `AcrPush` reachable from PR-triggered workflows. | The posture tradeoff is the owner's call. The mechanical half was doable and was done. |
| **P1-14 — citations are presentation, not provenance.** ~~A citation is rendered from what the model emitted; nothing binds a claim to the span it came from.~~ **Span-level half closed 2026-08-07 (#309).** Library Tier-2 mints a server-owned span id per injected excerpt (with content hash, retrieval timestamp, and a persisted verbatim excerpt), the registry lands on the answer, and a cited id that was never retrieved is caught and rendered distinctly. `untrusted_context` remains a *turn-level* taint bit and is still not claimed as more than that. | What remains: **claim-level support** (does the cited span actually say it), and attesting the other context sources — web search, recalled memory, session-uploaded documents — which are still cited as prose. | Claim support is entailment. Every check cheap enough to run inline is approximate, and this finding's own logic is that a partially-trustworthy badge is worse than none — so the honest version needs an evaluation set and a measured threshold, not a heuristic. | The span half was mechanical once the provenance survived prompt assembly, and was done. The claim half was refused rather than approximated. |

### What P1-14 and P1-16 actually mean

Both are described above in the language of the audit. In plain terms, and with
the reason each gets *worse* rather than better as traffic grows:

**P1-14 — a citation is a claim the model made, not a receipt.** *(Span-level half
closed 2026-08-07, #309; described here as it stood.)* When an answer says
"according to the Q3 filing", the app renders that because the model wrote it.
Nothing checks that a retrieved span actually says it, and nothing records which
span the sentence came from. A correct citation and a fabricated one are
byte-identical to the system. Prompt instructions can ask the model to cite well;
they cannot verify that it did. Today the exposure is small because
volume is small. It scales badly in a specific way: the cost of an unverifiable
citation is paid by the *reader*, so a system that produces ten a day and one
that produces ten thousand a day fail identically per answer, and the second one
fails ten thousand times. The fix is span-level provenance carried from retrieval
through prompt assembly into rendering — genuinely multi-week, with no safe
partial, because a *partially* trustworthy citation badge is worse than none.

> **What shipped, and where the line was drawn.** Library retrieval spans now
> carry a server-minted id, a hash of the exact injected text, and a persisted
> verbatim excerpt; an answer citing an id that was never retrieved is caught
> with certainty and shown as unverified. The *claim-level* half — does the cited
> span support the sentence — was deliberately not built, because the only checks
> cheap enough to run inline are approximate and this entry's own reasoning says
> an approximate badge is worse than none. The excerpt is shown to the reader
> instead of a verdict the app is not entitled to reach.

**P1-16 — the tool loop now token-streams.** This was open when the section was
written. It closed in #307: every model iteration streams, fragmented tool calls
are reassembled under bounds, and terminal persistence/cancellation/error
framing remain pinned. Measured through real uvicorn with a paced model, median
time-to-first-token fell from 1247.9 ms to 31.7 ms (39.3x) while total turn time
stayed unchanged. #312 then added the sibling-merge interaction coverage that
proved citation attestation survives every new tool-loop persist path.

> **On sequencing.** Measured production usage over the 30 days to 2026-08-06 was
> 23 chat turns across 4 active days, 10 tool invocations (all `remember_memory`),
> and zero `browse_url` / `web_search` / `run_code`. That is why neither is urgent
> *then*. Both mechanical halves are now shipped: P1-16 is closed, and P1-14
> has airtight span-level receipts. P1-14's claim-level entailment and non-library
> provenance remain deliberately open rather than approximated.

Other items that are **contained rather than closed**, recorded so the containment is
not mistaken for a fix:

- **P1-10 — sharing is still not tenant-aware.** `Visibility.public` grants read to
  any authenticated caller without comparing tenants. Startup now refuses when more
  than one Entra tenant is allowed, so the latent bug cannot be activated by
  appending a GUID to a variable — but making it genuinely multi-tenant means
  persisting the owner's tenant and comparing it in `library/access.py`, or renaming
  the visibility to `application_public`.
- **P1-13 — closed for the capabilities that matter; unattended runs remain
  exempt.** Per-invocation approval now covers MCP tools *and* the 15 first-party
  synthetic capabilities, which get their risk from
  `agents/synthetic_governance.py` rather than from the registry. `browse_url` and
  `run_code` are held on every turn; the four searches, media generation,
  `remember_memory` and `export_document` are held only on a turn that carried
  untrusted content; the four read-only capabilities and `delegate_to_agent` are
  not held. `test_ungated_capabilities.py` pins that split in both directions, and
  an unclassified capability is refused at runtime. What is still open: a
  **workflow step** runs unattended, so there is nobody to ask and
  `workflows/runner.py` opts out with an explicit `ApprovalPolicy.off`. Closing
  that needs a durable, out-of-band approval channel for unattended runs — a
  product feature, not a flag flip.

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
  requires the 17 always-emitted status checks. No bypass actors, so it applies to
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
  [greenfield standup guide](./runbooks/greenfield-standup.md).
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
  it converts silence into a loud 422 but moves the agent onto the tool path. That
  reasoning predates P1-16: since the tool loop streams, the streaming cost is gone
  and only the extra round trips remain, so this tradeoff is worth revisiting.
- **The official-MCP plane is now verifiable.** `GET /api/admin/metrics/official-mcp`
  performs full MCP discovery (`initialize` → `tools/list`), not a ping — because the
  handshake returns 200 even when the upstream toolbox does not exist, so every
  ping-style check reported healthy while the plane served nothing.

[`CHANGELOG.md`](../CHANGELOG.md) tracks changes from this point forward. It does not yet
carry the project's earlier history: this repo has no reviewed release tags to backfill
from, and the changelog's own policy is not to invent them.
