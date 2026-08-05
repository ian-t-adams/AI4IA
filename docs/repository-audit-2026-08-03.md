# Repository audit - 2026-08-03 (continued 2026-08-04, disposition updated 2026-08-05)

## Audit status

**Overall verdict:** AI4IA is a substantial, well-tested governed chat platform,
not a prototype. Its strongest claims - catalog-driven models, gateway-first
compatible traffic, user-scoped canonical state, server-side authorization, rich
chat/agent/document/voice surfaces, and an azd/Bicep deployment spine - are backed
by real code.

It is not yet safe to describe the repository as a production-complete enterprise
platform or a one-command clean-room deployment. One confirmed credential-handling
defect requires immediate incident response. Several other boundaries rely on
cooperative clients, manual deployment work, mutable builds, or documentation that
has drifted from the implementation and live environment.

> **Written 2026-08-03. See [the disposition](#immediate-action--status-as-of-2026-08-05)
> for what has since been fixed** — the credential defect is fixed and the key is
> rotated, but the production-completeness verdict above still stands: the
> deployment-posture findings (P1-3, P1-4, P1-7) are untouched.

A second phase exercised the public deployment in a real browser, ran a controlled
HTTP/SSE chat round trip through the actual FastAPI application, audited performance
and Responsible-AI behavior, traced complete user journeys, and checked API/web/docs
contracts. That evidence proves the core API and persistence path work under controlled
dependencies. It does **not** prove the signed-in production experience or live Azure
model, search, document, tool, workflow, media, and voice data planes end to end.

| Dimension | Assessment |
| --- | --- |
| Claimed versus served capability | **Mostly implemented, materially partial at trust, durability, and operator seams** |
| Backend/security | **Strong foundations; one critical proxy disclosure and several high-risk boundary gaps** |
| Web/UX | **Feature-rich and well tested; important truth, destructive-action, and accessibility defects remain** |
| Documentation | **Excellent depth; current-state contradictions and unsafe recovery guidance reduce trust** |
| Infrastructure as code | **Conditionally reproducible for a demo; not a production-safe one-command rebuild** |
| CI/CD and operations | **Strong static validation; weak artifact provenance and production closure** |
| Responsible AI/effectiveness | **Strong execution controls; harmful-content blocking, provenance, consent, and outcome proof are incomplete** |
| Performance/scalability | **Bounded and suitable for low-volume demos; no scale proof and several confirmed memory/latency multipliers** |
| Agent/human comprehensibility | **Good maps and invariants; oversized modules and incident-heavy docs impose high cognitive load** |

### Immediate action — status as of 2026-08-05

> **This section is the audit's living disposition. The findings below it are
> preserved as originally written (2026-08-03/04) and are NOT edited when they
> are fixed — an audit that quietly rewrites itself cannot be audited. Check
> here for current status.**

**1. Proxy-ingress credential disclosure (P0-1) — FIXED and the key is ROTATED.**

`ProxyConfig.ValidateAuthKey1`, populated with the proxy-ingress APIM
subscription key, was emitted unredacted in a startup custom event because the
redactor matched key paths by substring and `Key1` matches none of its terms.

- Fixed in #266: an explicit `ConfigOptionAttribute.Secret` flag, both auth keys
  marked, and a canary-based regression test (`ConfigRedactionTests`) that fails
  if any secret-marked option reaches the serialized event. Mutation-tested:
  removing the flag makes three tests fail.
- The key was **rotated in production on 2026-08-04** with zero downtime, using
  the proxy's `ValidateAuthKey2` slot for dual-accept during switchover. The old
  key was verified to return 403 and the new key 200. Procedure recorded in
  [`runbooks/key-rotation.md`](./runbooks/key-rotation.md).

**Severity correction, measured rather than assumed.** This finding was written
on the assumption that the event reached durable telemetry. It did not, in this
deployment: `EVENT_LOGGERS` is unset, so the config event goes to the default
*file* client (`eventslog.json`) inside the container, which is ephemeral and
dies with the revision. Searches of Application Insights (`traces`,
`customEvents`, `exceptions`) and of `ContainerAppConsoleLogs_CL` found **no
occurrence of either key**. The leak path was real and is fixed; its blast radius
was narrower than this section originally implied. That depends on
`EVENT_LOGGERS` and `AppInsightsConnectionString`, both configurable — re-check
before relying on it.

Ordering constraint worth knowing: the redaction fix must be *deployed* before a
rotation, or the new key is written where the old one was.

**2. Annotation-only content policy (P0-2) — ACCEPTED by the owner, with
compensating visibility.**

Every harm, jailbreak and protected-material filter remains enabled and
non-blocking. This is a deliberate owner decision, not drift.

The compensating control added in #266: Foundry returns a verdict for every
category on every turn, and the application previously **discarded all of it** —
so the safety system ran on every request and was completely invisible. Those
annotations are now normalized, persisted on the message, and shown in a
collapsed per-turn panel that states plainly that nothing was blocked or
rewritten. `filtered: true` is always treated as notable, so flipping the policy
to blocking would surface rather than change behaviour silently.

**Still outstanding for this item:** a written Responsible-AI decision record
(accountable owner, scope, expiry, review cadence, compensating-control
assessment). A code comment and a CI test are not an approval record.

### Disposition of the P0/P1 findings

Verified against the tree at `main` on 2026-08-05, not from memory.

| Finding | Status | Where |
| --- | --- | --- |
| P0-1 proxy credential in startup event | **Fixed + key rotated** | #266, live rotation |
| P0-2 annotation-only filters | **Accepted**; annotations now surfaced | #266 + owner decision |
| P1-1 client can override server-owned model fields | **Fixed** | #266 |
| P1-2 Code Interpreter retention/metering | **Partially fixed** — `store:false` locked; entitlement/usage accounting still absent | #266 |
| P1-3 fresh azd deploy is public + dev auth | **Open** — re-verified 2026-08-05: `apiAllowDevAuth` defaults **true** in `main.bicep:70` and reaches the container, so stock defaults *serve* with client-controlled `X-Dev-User` identity. Only `appEnvironment == 'prod'` forces it closed | — |
| P1-4 gateway-only routing is convention, not IAM | **Open** — `disableLocalAuth` still defaults false | — |
| P1-5 APIM key is a non-secure output | **Fixed** — compiled ARM emits `securestring` | #266 |
| P1-6 no post-deploy proof or rollback | **Fixed** — pre-deploy revision capture, hard rollout/health/web/proxy/domain assertions, an authenticated gateway canary, and automatic rollback. Does not cover a cancelled run or job timeout | #274 |
| P1-7 tested artifact is not the deployed artifact | **Open** | — |
| P1-8 teardown destroys data it cannot restore | **Fixed** — `-Force` now requires `-AcknowledgeDataLoss` and refuses before any `az` call; `capture-data-recovery-state.ps1` records the Cosmos restorable-instance id, a blob manifest, and secret names. Blob still has no restore path — the capture makes that a decision rather than a discovery | #266, #273 |
| P1-9 sharing read failure can revoke an ACL | **Fixed** | #266 |
| P1-10 "tenant-public" is application-public | **Open** — latent until a second tenant is onboarded | — |
| P1-11 portal presents stale evidence as live health | **Fixed** — `healthy` now requires a positive Resource Health signal | #266 |
| P1-12 hard-coded colors bypass theme tokens | **Fixed**, and the severity was understated: measuring the literals made this an accessibility defect, not a style nit. `#fff` on a `var(--accent)` fill is **1.07:1** in the high-contrast theme and sat on three panels' primary action button. All 14 literals now resolve through tokens, `--warn` was added, and two gates enforce it | #266, #271 |
| P1-13 indirect prompt injection drives preapproved MCP | **In review** — approval is bound to an argument digest recomputed at dispatch, which is sound; returned for three defects found in review (one grant authorized up to 8 executions; the preview card silently drops attacker-chosen keys; the single-use burn is a non-atomic read-modify-write) | #272 |
| P1-14 citations are presentation, not provenance | **Open** | — |
| P1-15 admin refresh can exhaust a 1 GiB replica | **Fixed** — the dashboard is served from one projected ledger scan instead of seven | #270 |
| P1-16 live-default chat is not token-streaming | **Partially fixed** — proxy now flushes per SSE event; the non-streaming tool loop remains | #266 |
| P1-17 region/data-zone constraints silently relax | **Fixed**, and the residency model was corrected: `residency` now derives from the deployment SKU, not endpoint geography | #266, #267, #268 |

Selected P2 items also closed: CGNAT SSRF gap, portal service counts, portal
narrow-screen reflow, the missing `main` landmark, the APIM compiler harness
rejecting its own shards, and two documentation current-state contradictions.

**What is still open, and why.** Four of the five remaining items are one decision,
not four: `main.parameters.json` still defaults to `${AI4IA_APP_ENVIRONMENT=dev}` and
`${AI4IA_AUTH_PROVIDER=dev}`, and `apiAllowDevAuth` defaults `true`, so a stock
`azd up` serves publicly with client-controlled identity (mechanism verified in
[P1-3](#p1-3-a-fresh-azd-deployment-is-public-dev-authenticated-and-expensive)).
That single default is the root of P1-3, and it is what makes P1-4 (gateway-only
routing is convention, not IAM — `disableLocalAuth` still defaults false) and P1-7
(the tested artifact is not the deployed artifact) matter as much as they do. They
need production-versus-demo deploy profiles, which is a posture decision for the
owner rather than a defect to patch.

The other two are feature work with no safe shortcut: P1-10 (tenant-public means
application-public) stays latent until a second tenant is onboarded, and P1-14
(citations are presentation, not provenance) needs real span-level provenance —
`untrusted_context` in the new approval work is a *turn-level* taint bit and is
deliberately not claimed as more than that.

Two items are honestly partial rather than done. P1-2 locks `store: false` but has no
entitlement or usage accounting for Code Interpreter, and P1-16 fixed the proxy's SSE
buffering while the non-streaming tool loop remains. Both are recorded as partial in
the table above rather than rounded up.


## Scope and method

The audit covered all 815 tracked files at commit
`92aeda6e4662780fcaf08915723e20c20572b1cb`:

- `app/api`: FastAPI routes, services, persistence, auth, tools, memory, documents,
  usage, metrics, voice, tests, package metadata, and image definition.
- `app/web`: Next.js routes, components, API helpers, state transitions, tests,
  accessibility, themes, package metadata, and image definition.
- `infra` and `azure.yaml`: every Bicep module, parameter/catalog/schema/policy file,
  azd service, hook, identity, role, output, and feature path.
- `.github`, `scripts`, and `proxy`: CI/CD, security scans, operator scripts,
  generated-artifact gates, Docker boundaries, and AI4IA's vendored-proxy changes.
- Root governance files, `docs`, `site`, `foundry`, and brand assets.

Five initial domain passes were reconciled against repository-wide route, feature,
claim, and generated-catalog inventories. The continuation added focused exploit,
performance, Responsible-AI/effectiveness, UX-journey, and live-browser passes plus
independent runtime/contract probes. High-impact findings were traced again through
their complete data path before inclusion. Findings distinguish a defect from an
intentional/default-off feature, an asserted exception, and an accepted tradeoff
already recorded in [`roadmap.md`](./roadmap.md).

No live Azure mutation, credential read, destructive script, deployment, or external
Foundry provisioning operation was executed.

## Validation evidence

| Check | Result |
| --- | --- |
| Model/MCP/voice/gateway generated drift | Passed |
| Model catalog consistency | Passed: 39 models, 71 deployments, 3 regions |
| Feature prerequisite validator | Passed with warnings for placeholder owner/publisher and no budget recipient |
| Documentation catalog completeness | Passed |
| Operational/script unit suite | Passed: 181 tests |
| API lock check | Passed |
| API Ruff | Passed |
| API Pyright | Passed: 0 errors |
| API Pytest | Passed: 2,141 tests; one upstream TestClient deprecation warning |
| Measured line coverage | **Not obtained:** coverage tooling could not be downloaded through the PyPI TLS boundary; a stdlib full-suite tracer was abandoned after orders-of-magnitude slowdown, so no misleading percentage is reported |
| Cosmos migration tests | Passed: 6 tests |
| Bicep compilation | Passed with five warnings (BCP318 x2, BCP187, BCP329 x2) |
| Proxy build | Passed with one nullable warning |
| AI4IA proxy tests | Passed: 14 tests |
| Web test/lint/build | **Not executed:** dependency restoration failed at the npm registry TLS handshake on Node 22 Alpine, Node 22 Debian, and the host; no repository test had started |
| OpenAPI/web contract | Passed: 93 backend routes including WebSocket; all 55 production web API path literals matched a backend route |
| Local documentation links/assets | Passed: 83 Markdown links plus 85 local HTML assets; no missing file or anchor |
| Controlled local API HTTP/SSE | Passed: health, session CRUD, ownership isolation, streamed chat, terminal persistence, and usage accounting |
| Public browser exploration | Public app, portal, proxy ingress, and Microsoft sign-in redirect reached; signed-in product flows not entered |
| Proxy production image | Built; non-root UID 1654; `/health` returned 200 on the proxy's standalone default port |
| API/web production images | **Not completed:** PyPI/npm TLS negotiation failed before application build steps; this is an environment limitation, not a repository defect |

The passing suites are meaningful but do not negate the findings below. Several are
outside current test assertions, and the web suite could not be refreshed in this
environment.

## Claimed capability versus served ability

| Capability | Served status | Evidence-based assessment |
| --- | --- | --- |
| Multi-model | **Implemented** | Catalog contains 39 models and drives 71 regional deployments and runtime data. |
| Multi-region | **Partial** | Model deployments and APIM routing span three regions. APIM, Container Apps, Cosmos, Search, storage, and the browser/API plane are single-region. |
| HTTP/SSE gateway path | **Implemented with a critical secret defect** | FastAPI -> SimpleL7Proxy -> APIM -> Foundry is wired. Proxy startup telemetry exposes the ingress key. |
| Realtime/Voice Live | **Implemented, partial UX/proof** | FastAPI -> APIM WebSocket relay enforces auth/origin/ownership. Some advertised voice agent/tool controls are not rendered, and the signed-in microphone canary remains open. |
| Entra authentication | **Implemented** | Signature, issuer, audience, tenant, and expiry validation exist. JWKS rotation/outage behavior and web misconfiguration handling need hardening. |
| Dev authentication | **Implemented, unsafe clean-room default** | The same-origin proxy boundary is coherent, but checked-in azd parameters deploy public `dev` auth unless overridden. |
| Sessions and chat | **Implemented, boundary partial** | Streaming and terminal persistence are strong. Arbitrary `params` can replace reserved model request fields, and request sizes are unbounded. |
| Grounding and citations | **Partial** | Document, memory, Web IQ, and web context are bounded and fenced, but retrieval failure is silent and citations are not tied to a server-owned evidence record. |
| Content safety | **Critical intentional gap** | Foundry harm, jailbreak, and protected-material filters annotate but never block; the application does not consume those annotations or provide an equivalent enforcement layer. |
| Agents and built-in tools | **Implemented** | User ownership, allowlists, execution-time authorization, budgets, aliases, and redaction are real. |
| BYO MCP | **Implemented, default-off, partial approval** | Key Vault secret storage, public-HTTPS validation, DNS revalidation, and IP pinning exist. Per-invocation human approval is not exposed; CGNAT is not rejected. |
| Official MCP/Foundry Toolbox | **Partial/manual** | APIM and runtime discovery exist. Toolbox/API Center assets require operator scripts and can drift from IaC. |
| Workflows | **Implemented, partial tool contract** | Sync and Durable Task execution exist. Server validation accepts/inherits tools the workflow runner cannot execute; the web warns but does not make the server authoritative. |
| Durable workflows | **Implemented and enabled by the checked-in azd parameter default** | Scheduler-backed runs are owner-scoped. Live state was not revalidated; documentation describes the template/live posture inconsistently, and timeout termination depends on polling. |
| Memory | **Implemented, partial UX** | Cosmos text/vector memory, ETags, epochs, CRUD, recall, and deletion fencing are strong. Global consent and recall provenance remain intentional gaps. |
| Documents/library | **Implemented, partial durability** | Upload, parsing, Content Understanding, retrieval, sharing, annotations, analyzers, media, and compute APIs exist. Enrichment is process-local and custom analyzer config is stored but not applied. |
| Images and video | **Implemented, partial artifact durability/metering** | Provider calls and authenticated artifacts exist. In-memory fallback is advertised as available, and storage failure can skip metering after paid generation. |
| Code Interpreter | **Implemented direct exception, governance partial** | Direct Responses/Files calls are real. Library compute omits `store:false` and detailed entitlement/usage accounting. |
| Web IQ | **Implemented, deployment-dependent** | Governed server-side tools and health exist. Checked-in parameters enable it without proving managed-identity entitlement or an API key. |
| Usage/entitlements | **Implemented, observational/soft** | User-scoped ledger and admin reports exist. Usage writes are best-effort and numeric enforcement intentionally fails open. |
| Admin telemetry | **Implemented, partial truth** | Server-owned metrics/KQL and partial/unavailable states exist. Some web panels convert failure/unknown cost to empty or zero. |
| Foundry A2A runtime | **Not present** | Repository scripts generate/validate projections; FastAPI does not execute remote A2A agents. |
| Foundry routine runtime | **Not present, intentional** | Current SDK path is validation/planning-only, despite conflicting wording in schema/example docs. |
| Clean-room `azd up` | **Conditional demo reproduction** | Core resources deploy from Bicep. Identity, quota/model access, provider checks, domains, Toolbox/API Center assets, and production posture require additional work. |
| Self-documenting portal | **Implemented, stale/incorrect operational truth** | Docs catalog is complete and generated. Status, requirements, architecture, feature posture, and per-service live counts contain drift; mobile navigation does not reflow. |

## Does the application work?

### Controlled end-to-end proof

A local fake gateway emitted real HTTP/SSE frames while the unmodified FastAPI
application ran with its in-memory repositories. The test did not call Azure or use
production credentials, but it exercised the application boundary rather than mocking
the route:

| Observation | Result |
| --- | --- |
| Liveness | `{"status":"ok"}` |
| Session lifecycle | Create, list, patch, read, and delete succeeded |
| Ownership isolation | A second dev user received 404 for the first user's session |
| Chat stream | Five SSE frames including server metadata, two content deltas, usage, and `[DONE]` |
| First frame | 7.82 ms against the local deterministic gateway |
| Whole turn | 524.72 ms |
| Canonical messages | `user/complete`, `assistant/complete`; assistant text `Audit smoke passed.` |
| Usage | 10 prompt + 3 completion tokens, 140 micro-USD, complete correlation/deployment/region metadata |
| Provider request | Catalog deployment path plus server-built history, stream options, and normalized generation parameters |

This proves the core request -> routing -> SSE -> persistence -> usage path is coherent
when its dependencies behave. It does not validate Azure latency, model quality,
content filtering, APIM policy behavior, Cosmos/Search RU posture, or production Entra
claims.

### Deployed public surface

The app returned 200, used a per-request nonce CSP, HSTS, frame denial, MIME sniffing
protection, strict-origin referrers, and a constrained permissions policy. The
same-origin model endpoint returned the expected 401 without a bearer token, the public
model proxy returned the expected 403 without its ingress credential, and the sign-in
button redirected to Microsoft's hosted login without credentials being entered.

Three lightweight public probes averaged 229 ms for the app, 190 ms for the protected
model route, 197 ms for the protected proxy, and 54-57 ms for the GitHub Pages portal
from this audit host. These are reachability samples, not an SLO or load test.

A single cold Playwright navigation observed 15 app resources, 361 KB transferred,
1.22 MB decoded, and about 4.14 s navigation. Portal home transferred 29 KB and took
about 2.30 s cold/281 ms repeat; the architecture page additionally loaded about
753 KB of Mermaid. These one-run browser samples locate obvious weight but are not
statistically valid performance baselines.

### Honest verdict

| Question | Answer |
| --- | --- |
| Does the core API work? | **Yes under controlled dependencies.** |
| Is the public deployment reachable and enforcing its pre-auth boundaries? | **Yes for the surfaces safely tested.** |
| Does the complete signed-in production product work? | **Unproven.** Authentication prevented safe chat/agent/workflow/admin/voice exploration. |
| Does live Azure model/search/document/tool/media/voice behavior work well? | **Unproven.** No production data-plane canary was authorized. |
| Can the proxy image run? | **Yes**, but standalone `docker run -p ...:8080` misses the listener unless `Port=8080`; Bicep supplies that env value. |
| Can another tenant reproduce the demo? | **Conditionally**, with manual identity, quota, domain, data-plane, and operator steps. |

## Prioritized findings

### P0 - critical

#### P0-1: Proxy startup telemetry discloses the proxy-ingress credential

The deployment binds the primary key to `ValidateAuthKey1`
(`infra/modules/gateway.bicep:624-625,688-696`). That maps to
`Profiles:Auth:Key1` (`proxy/SimpleL7Proxy/Config/ProxyConfig.cs:79-84`).
`OutputEnvVars` enumerates every configuration property and emits it in a custom event
(`ConfigFactory.cs:323-355`), but its redactor only recognizes names containing
`connectionstring`, `password`, `secret`, `token`, `apikey`, or `sas`
(`ConfigFactory.cs:360-371`). `Key1` matches none.

`ProxyEvent` copies every property into event-client JSON and Application Insights
custom-event properties without a second filter
(`proxy/SimpleL7Proxy/Events/ProxyEvent.cs:198-205,241-310`). Custom events are enabled
for the event client by default (`ProxyConfig.cs:62-67`), whose default implementation
writes `eventslog.json`.

This is a confirmed disclosure path, not a hypothetical naming concern. `Key2` is
empty today but would be exposed identically if configured. Existing proxy tests cover
key comparison/auth parsing, not startup serialization or redaction.

#### P0-2: Every deployed model has annotation-only harm and jailbreak controls

`infra/modules/foundry.bicep:59-85` sets `blocking:false` for hate, sexual,
self-harm, violence, jailbreak/Prompt Shield, and protected-material filters on the
prompt and/or completion. The policy name flows into every deployment
(`infra/main.bicep:1029-1034`, `infra/modules/models.bicep:11-33`), and
`scripts/tests/test_rai_policy.py` deliberately fails if blocking returns.

This is intentional, not drift. Annotations remain enabled, but the application does
not consume them or provide an independently enforced moderation/escalation boundary.
Severe harmful or jailbroken output can therefore reach users. The test calls the
posture an approved guardrails-modification exception, but no repository ADR records
the approver, scope, rationale, compensating controls, review/expiry date, or rollback
conditions.

An accountable human must re-evaluate the exception. Either restore use-case-appropriate
blocking or add an independently tested enforcement layer. Release evaluation should
cover every text, image, video, and voice model, block/escalate every agreed severe
case, measure benign over-refusal, and prove no unsafe tool action can occur.

### P1 - high

#### P1-1: Authenticated callers can override server-owned model request fields

`ChatRequest.params` is an unrestricted dictionary
(`app/api/src/ai4ia_api/routers/chat.py:91-99`). The normal web UI sends only
temperature, top-p, token limit, and reasoning effort, but FastAPI is the trust
boundary and direct callers are supported.

Chat Completions builds `{"messages": server_history, **params}`, so caller-supplied
`messages` wins (`gateway/client.py:307-337`). Responses builds `model`, `input`, and
`store:false` before spreading normalized caller parameters, so a caller can replace
the deployment/input and restore provider storage (`gateway/client.py:339-375`).
Plain turns can also forward provider tools outside the app's tool registry.

Use a typed allowlist with `extra="forbid"` and construct all reserved fields after
caller parameters. Reject `messages`, `model`, `input`, `instructions`, `store`,
`tools`, `tool_choice`, `parallel_tool_calls`, `stream`, and `stream_options`.

#### P1-2: Direct Code Interpreter requests create an ungoverned second data/spend path

Library Code Interpreter calls omit `store:false`
(`app/api/src/ai4ia_api/code_interpreter/client.py:192-208`), while the normal
Responses gateway explicitly disables provider retention. Library compute is also
built without entitlement or usage services
(`library/compute_factory.py:94-123`) and records no synthetic usage for up to three
compute executions per tool turn (`library/compute_capability.py:54,187-198,282-297`);
each execution can upload, run, and delete provider resources.

Lock `store:false` server-side, meter every provider attempt, and perform an
entitlement check before each direct call. Add a distinct target identity so direct
compute cannot hide inside the parent chat charge.

#### P1-3: A fresh azd deployment is public, dev-authenticated, and expensive

`infra/main.parameters.json:32-37` defaults the app environment and auth provider to
`dev`, while API/web/proxy ingress is public. The same file hard-enables official MCP,
Toolbox/API Center, image/video, document understanding/compute, raw-file compute,
search, Voice Live/tools, summarization, custom tools, and Web IQ
(`infra/main.parameters.json:65-180`).

> **Re-verified 2026-08-05 and the exact mechanism recorded, after a first attempt at
> "correcting" this finding was itself wrong.** Reading `config.py` alone suggests the
> stack is fail-closed: `validate_runtime()` refuses dev auth unless
> `dev_auth_permitted`, which is `env == local or allow_dev_auth`, and `allow_dev_auth`
> defaults to `False` *in code*. It is not fail-closed, because infra overrides that
> default. The chain, all on stock values:
>
> - `infra/main.bicep:70` — `param apiAllowDevAuth bool = true`, and it is **not**
>   present in `main.parameters.json`, so the Bicep default is what applies.
> - `infra/main.bicep:845` —
>   `allowDevAuth: appEnvironment == 'prod' ? false : apiAllowDevAuth`. With
>   `appEnvironment` defaulting to `dev`, this evaluates to `true`.
> - `infra/modules/api.bicep:731-732` — that value is injected as the container
>   variable `AI4IA_ALLOW_DEV_AUTH`.
>
> Constructing `Settings` from that exact container environment:
> `allow_dev_auth=True`, `dev_auth_permitted=True`, `auth_provider_is_spoofable=True`,
> and `validate_runtime()` **passes**. The app starts and serves, deriving identity
> from the client-supplied `X-Dev-User` header on public ingress.
>
> `appEnvironment == 'prod'` is the only thing that forces it closed, and `prod` is
> not the default. The lesson recorded for the next reader: a defaulted `False` in
> `config.py` proves nothing on its own, because the deployment layer sets the
> variable. `app/api/tests/test_deploy_defaults_fail_closed.py` now pins this
> composition so it cannot drift silently in either direction.

This is suitable only as an explicitly labeled demo profile. Introduce
`demo|production` profiles. Production must fail unless Entra, owner, publisher,
budgets/alerts, durable storage, and other prerequisites are complete; it must never
permit dev auth in Azure. Flipping `apiAllowDevAuth` to default `false` would close
the immediate exposure, at the cost of making a no-Entra demo deploy fail at startup
instead of serving — which is the tradeoff the profile split exists to make explicit.

#### P1-4: Gateway-only routing is a code convention, not an IAM boundary

Foundry local authentication remains enabled
(`infra/modules/foundry.bicep:14-15,37-41`), and the API identity receives account-wide
OpenAI/Cognitive Services User roles across regional accounts
(`infra/main.bicep:340-344`, `infra/modules/foundry.bicep:89-112`).

Disable local auth. Move the Code Interpreter/native exception into a separately
deployed workload with exclusive identity/account access, then remove direct compatible
model roles from the main API identity. Attaching another managed identity to the same
FastAPI container is not isolation because any code in that workload can request its
token.

#### P1-5: An APIM MCP subscription key is a non-secure deployment output

`infra/modules/apimcore.bicep:102-104` suppresses the secret-output linter and exports
`listSecrets().primaryKey` as a normal string. That can expose the product-scoped key
through deployment history. Mark the output `@secure()`, preserve secure typing across
every module boundary, and fail CI on any non-secure `listSecrets()`/`listKeys()` output.

#### P1-6: Deployment has no post-deploy proof or rollback

`azure.yaml` postprovision runs after infrastructure provisioning, before application
deployment, and intentionally treats application probes as best-effort. The GitHub
workflow ends immediately after `azd deploy`
(`.github/workflows/deploy.yml:338-345`).

A bad image, secret binding, runtime import, or proxy/APIM path can therefore complete
successfully. Add enforced checks for revision/digest rollout, API live/ready, web,
proxy readiness, an authenticated proxy -> APIM -> Foundry canary, and custom domains.
Capture the previous revision and restore it on application failure.

#### P1-7: The tested artifact is not the deployed artifact

CI validates source and separately builds images, while azd rebuilds later from mutable
base tags. The API image installs dependency ranges from `pyproject.toml`, not
`uv.lock`; downloaded build tools lack checksum verification; final images have no
SBOM/signature/provenance or blocking image scan.

Build once, test/scan/sign it, and deploy immutable digests. Use a frozen lock-derived
Python install and digest-pin runtime bases.

#### P1-8: The teardown runbook overstates recovery completeness

The runbook inventories resources, deletes the resource group, and purges soft-deleted
services (`docs/runbooks/teardown.md:24-49`), then calls inventory plus `models.json`
the recovery source (`teardown.md:65-67`). Those inputs reconstruct infrastructure, not
canonical data. Cosmos is materially better protected than that sentence implies:
`deployment.md:785-842` documents Continuous7Days point-in-time recovery and a
successful restore experiment. The teardown path still does not capture/integrate a
restore timestamp/account, and inventory cannot recover Blob source documents/artifacts
or Key Vault secrets after purge.

Split **data-preserving infrastructure rebuild** from **full environment disposal**.
Require explicit RPO/RTO and destructive approval, wire the existing tested Cosmos
point-in-time procedure into the teardown/recovery workflow, add Blob recovery/export,
a Key Vault recovery decision, external DNS/Entra inventory, and evidence stored outside
the target subscription/resource group.

#### P1-9: A sharing read failure can revoke an unknown ACL

`SharePanel` initializes to private/empty, converts a failed ACL read into
`loading=false`, and still enables Save
(`app/web/src/components/SharePanel.tsx:64-84,109-128,371-375`). A transient GET
failure can therefore overwrite existing grants with defaults.

Use explicit `loading|ready|error`; never render an editable form or enable Save before
a successful authoritative read. Offer Retry.

#### P1-10: "Tenant-public" documents are application-public in multi-tenant auth

Configuration supports multiple Entra tenants (`app/api/src/ai4ia_api/config.py:647-649`),
but `library/access.py:26-40` grants `Visibility.public` to any authenticated caller and
does not compare tenant identity. Persist owner tenant and enforce tenant equality, or
rename the feature to `application_public` and document that cross-tenant behavior.

#### P1-11: The portal presents stale provisioning evidence as live health

The generated snapshot is dated 2026-08-01 and reports 33/33 healthy
(`site/data/status.js:1-12`). The page body does call the data a static snapshot
(`site/status.html:43-46`), but its title/navigation describe a live health view
(`site/status.html:31-35`, `site/index.html:110`). The generator maps Azure Resource
Health `Unknown` to `healthy` (`scripts/status-snapshot.ps1:163-174`), and the renderer
shows age without downgrading stale snapshots (`site/assets/app.js:89-100`).

Rename it **Deployment snapshot** unless a live monitor backs it. Show age, source
workflow, commit, resource-health state, and endpoint-probe time independently. Once
the freshness SLA expires, suppress aggregate healthy counts and mark the result stale.

#### P1-12: Hard-coded web colors violate the claimed theme/accessibility floor

Multiple component-local success/danger/accent colors bypass theme tokens in
`Pill.tsx:10-17`, `LibraryPanel.tsx:44-50`, `AnnotationsPanel.tsx`, `SharePanel.tsx`,
and `MediaPlayer.tsx`. Against the committed dark surface (`globals.css:59-112`),
`#15803d` measures 3.45:1 on `#161b22`, while `#b91c1c` measures 2.67:1. Current
contrast tests exercise CSS tokens only (`globals.contrast.test.ts:4,63-118`), not
these component literals.

Introduce semantic success/info/danger tokens for every theme and test rendered
component usages. Keep the high-contrast theme an accessibility surface, not a brand
surface.

#### P1-13: Indirect prompt injection can drive preapproved external actions

Memory and document blocks are randomized and explicitly untrusted, but they are still
promoted into system messages (`routers/chat.py:1125-1176`). Trusted or
`requireApproval=never` MCP tools execute without a live approval prompt, and tool
results return to the model as ordinary content. A hostile document, memory, web result,
or MCP response can therefore influence an outbound external-tool argument containing
other context. Fences reduce delimiter attacks; they are not an information-flow
boundary.

Add short-lived per-invocation approval for external/destructive calls, including tool,
destination, purpose, and redacted argument preview. Track context provenance and
require approval whenever outbound arguments derive from documents, memory, or another
tool. Red-team with canary data; the release threshold is zero unauthorized canary
egress.

#### P1-14: Citations are model presentation, not verified provenance

Persisted messages contain prose, attachments, and coarse activity but no immutable
claim-to-source registry (`sessions/models.py:99-128`). Web tools instruct the model to
cite URLs, and Markdown renders model-supplied links; media references resolve by
filename, where the first case-insensitive duplicate wins. The authored
`citation-discipline` skill is absent from the canonical toolbox's empty `skills` list.

Persist server-owned source IDs, document/version or URL, retrieval timestamp,
excerpt/span, and content hash per turn. Accept only citations to sources actually
returned and validate claim support. Filenames should remain display labels, not
identity.

#### P1-15: Admin refresh can consume most of a 1 GiB API replica

`AdminDashboard.tsx:684-717` launches seven usage reports concurrently. Every report
independently asks `AdminUsageService._fetch()` for up to 50,000 full ledger records
(`usage/aggregate.py:399-481`, `usage/cosmos_repo.py:66-90`). A read-only local
microbenchmark measured one 50,000-record `UsageRecord` list at 87.1 MiB; seven lists
imply roughly 610 MiB before Cosmos response buffers, SDK objects, the Python process,
and concurrent chat work.

Replace the fan-out with one server endpoint, one projected/streamed scan, and one-pass
rollups. Cache by window, materialize daily aggregates, paginate high-cardinality
results, and measure RSS, RU, dashboard p95, and chat latency at the 50,000-row cap.

#### P1-16: Live-default chat is not reliably token-streaming

Checked-in parameters enable web search. That routes ordinary Chat Completions turns
through a non-streaming tool loop and emits only one final delta
(`routers/chat.py:1564-1589`). Even direct SSE is passed through the proxy's
`JsonStreamProcessor`: its 4 KiB `StreamWriter` writes lines without per-line flush
(`proxy/Shared-parser/StreamProcessor/JsonStreamProcessor.cs:68-96`), while the periodic
flusher flushes only the underlying stream
(`proxy/SimpleL7Proxy/Proxy/StreamFlusher.cs:71-80`). A focused memory-stream probe
produced no bytes after ten 470-byte events until the writer itself was flushed.

Make web/tool use opt-in or intent-routed, stream tool and final-model progress, and
flush or replace the text processor per SSE event. Add an inter-chunk timing regression
test through the built proxy image.

#### P1-17: Explicit region/data-zone constraints silently relax

If an exact region or data-zone match is unavailable, catalog resolution silently
chooses the first deployment (`catalog.py:122-137`). Runtime data-zone labels are
derived from endpoint geography rather than SKU semantics (`catalog.py:140-159`), even
though Sweden Central commonly uses `GlobalStandard`
(`docs/region-capability-matrix.md:12-17`).

Reject unsatisfied explicit constraints before persisting the turn. Represent residency
from the deployment SKU's actual guarantee; do not label a GlobalStandard deployment
as EU-resident merely because its endpoint is in Sweden.

### P2 - medium

| Finding | Evidence/impact | Required improvement |
| --- | --- | --- |
| Web repeatedly turns failure into empty state | Sessions, agents, workflows, documents, analyzers/shared items, and admin panels often coerce rejected reads to `[]`. | Give each resource independent loading/error/empty/partial phases and Retry. |
| Unknown admin cost becomes zero | `costKnown` exists, but formatters and bars still render numeric zero. | Render `Unknown` or a labeled known subtotal. |
| `/clear` can leave deleted messages visible | `ChatApp.reconcileMessages` restores every old item absent from the authoritative response (`ChatApp.tsx:161-190`). | Preserve only explicitly tracked optimistic/in-flight messages. |
| Message failure/cancellation state is dropped | Status is in API types but omitted from display mapping. | Carry and visibly render partial/cancelled/error status. |
| Failed model save remains local truth | Optimistic model mutation swallows persistence failure. | Revert from the latest server snapshot and surface the failure. |
| Voice settings compute but ignore agent/tool preferences | Chat builds preferences that `VoiceSettingsPanel` does not type or render. | Render governed controls or remove the claim/state. |
| Entra web misconfiguration degrades to dev UI | Missing Entra fields disable MSAL rather than showing a configuration error; MSAL initialization lacks catch/cleanup. | Fail closed with error/retry and remove event callbacks on cleanup. |
| Destructive UI actions are inconsistent | Session/agent/workflow delete is one click; MCP/library deletion confirms. | Apply accessible confirmation or undo consistently. |
| Workflow tool contract is client-mitigated, not server-owned | Web filters direct chat-only tools and warns on inherited ones, but API accepts inert extra tools and runs incompatible agents. | Publish a workflow-specific server allowlist and fail requested-but-unavailable tools. |
| Document enrichment is not crash durable | Upload returns before process-local `asyncio.create_task`; recovery scans only `analyzing`, not `stored`. | Use a durable queue/outbox/job and recover every accepted state. |
| Artifact tools can be process-local | In-memory stores still make `/api/tools` report image/video available. | Require Blob outside local or report unavailable/durability explicitly. |
| Paid image/video calls can escape metering | Metering occurs after artifact persistence. | Record provider use once the provider succeeds, regardless of delivery outcome. |
| Turn acceptance is not idempotent | User messages persist before session patch/gateway/scheduler work; retries can duplicate dangling turns. | Add idempotency keys and explicit accepted/running/complete/error turn state. |
| JWKS rotation/outage handling is brittle | One-hour cache does not refresh unknown `kid`, single-flight refresh, or serve bounded stale keys. | Refresh once on unknown key, single-flight, stale-on-error, sanitized 503. |
| Raw upstream bodies reach clients | Gateway captures `resp.text` and chat forwards it. | Return fixed safe categories plus correlation ID; log bounded redacted detail. |
| `/health/ready` does not test readiness | It always reports OK and echoes configuration. | Probe mandatory dependencies with cached timeouts or rename and add real readiness. |
| MCP SSRF permits CGNAT | Filtering does not require `ip.is_global`; `100.64.0.0/10` passes. | Reject every non-global destination and expand special-range tests. |
| Per-turn MCP approval is absent | Chat has no short-lived approval object; users must persist trust/preapproval. | Bind signed approval to user/session/plane/tool/argument digest and expiry. |
| Correlation IDs are trusted verbatim | Arbitrary inbound value is persisted and forwarded. | Accept a short canonical format or mint a new ID. |
| Application plane is single-region/non-zonal | Models fail over regionally, but APIM capacity 1, ACA min 1, single-region Cosmos/Search, and LRS storage remain. | Define SLA/RTO/RPO before buying redundancy; then implement and exercise it. |
| Private-network mode is incomplete/unbound | Root flags are not in `main.parameters.json`; several services stay public. | Bind a supported profile and complete PE/DNS/egress/runner access as a whole. |
| RBAC exceeds least privilege | Web/proxy receive shared-vault/App Config access; API gets subscription Monitoring Reader and full secret-officer scope. | Split identities/vaults and mediate observability. |
| Model/version reconstruction is not exact | Model upgrade policy can follow new defaults. | Use deliberate version upgrades if byte-for-byte environment reconstruction is required. |
| Operator destructive scripts can misreport success | Native `az` exit codes are not consistently checked; purge uses subscription-wide substring selection. | Verify tenant/subscription, require exact resolved targets and inventory evidence, check every native exit code. |
| Model availability is not a deploy preflight | The script exists but deploy only registers providers. | Run quota/access checks before provisioning, with explicit steady-state opt-out. |
| Live APIM compiler harness rejects its own shards | Safety regex permits only catalog 0-3 while the harness defines 0-7. | Derive the guard from generated resources and behaviorally test every name. |
| Proxy changes sit in a security blind spot | C# is absent from CodeQL; proxy tree is broadly excluded from Trivy/gitleaks/NuGet management and image builds. | Scan first-party patches precisely and build/test/scan the production proxy image. |
| Portal/runtime requirements have drifted | Portal still says Python 3.14/Node 26 and omits current Durable Task/CI posture. | Generate requirements/module data from manifests, Dockerfiles, and Bicep. |
| Deployment/live facts lack provenance | Current-state claims repeat without run/commit/evidence links. | Publish one signed deployment-evidence record and reference it. |
| Documentation is incident-heavy | `AGENTS.md`, the 1,500+ line deploy runbook, roadmap, and unreleased changelog duplicate postmortems and live facts. | Move rationale to ADRs/postmortems and keep operator paths concise/current. |
| Product audience is inconsistent | README/portal say personal/demo; `PRODUCT.md` targets enterprise users/builders/admins. | Separate current deployment purpose from target product audience. |
| Retrieval failure is invisible | Blob/Search/embedding/context failures become an empty block and ordinary completion. | Persist/expose `used\|empty\|failed\|disabled`; fail document-required tasks instead of silently answering ungrounded. |
| Automatic memory lacks consent/provenance | Successful turns can save/recall memory without a global preference or answer-level source indicator. | Separate recall/write consent, require confirmation for sensitive candidates, and show used memory/version/correction controls. |
| Workflow prose can become false success | A required capability can fail to build while the model step still returns `ok=true`. | Preflight required capabilities and derive success from server-owned effect receipts, not model text. |
| Model catalog lacks behavioral capabilities | Tool calling, vision, structured output, citation, and grounding support are not encoded/probed. | Add measured capability fields and canary every advertised model/workload pairing. |
| No Responsible-AI release gate | CI has no groundedness, citation, prompt-injection, bias, safety, voice-subgroup, or task-success evaluation. | Add versioned evaluation datasets, thresholds, evidence retention, and blocking gates for high-risk regressions. |
| Personal-data retention is incomplete | Most canonical containers have no TTL and no account-level export/delete contract. | Document purpose/retention per store and verify active/derived/backup deletion behavior. |
| Admin usage fetch is repeated | Seven concurrent endpoints scan the same bounded ledger window. | Consolidate into one scan/response and pre-aggregate by day/window. |
| User-partitioned Cosmos queries omit routing | Several repositories filter by user but do not pass `partition_key`; session/message lists are unpaged. | Supply partition keys, projections, continuation tokens, and indexes that exclude large non-query text. |
| Auxiliary model calls are sequential/under-metered | Recall, RAG, implicit memory planning, and embeddings add latency and can escape centralized usage accounting. | Meter in the gateway client, parallelize independent bounded preparation, and queue post-turn memory work durably. |
| Ingestion holds large uploads in API tasks | Up to 50 MB is buffered and retained by unbounded process-local enrichment tasks. | Stream to Blob, enqueue references, cap concurrency, and move work to a durable worker/job. |
| Voice transcript save is N+1 | Up to 200 turns are upserted sequentially and each upsert repeats ownership/session work. | Verify once and use deterministic point reads/transactional batches. |
| Long chat is O(n) per token and near O(n²) in prompts | Full history is loaded/rendered; every delta rebuilds/sorts messages, reparses Markdown, and restarts smooth scrolling. | Separate paged UI history from bounded model context; buffer per frame, memoize, virtualize, and autoscroll only near bottom. |
| Inputs and output ceilings are unsafe defaults | Chat content is unbounded and a 1,024-token client default can expand to a model's 128,000-token ceiling. | Enforce byte/token caps and require explicit opt-in for very large outputs. |
| MCP size limit applies after buffering | A response is fully materialized before the 2 MB check, with a new client/handshake per call. | Stream-abort at the limit, inspect `Content-Length`, reuse bounded per-turn sessions, and cap wall time. |
| Media is fully buffered repeatedly | Up to 200 MB can be materialized provider -> API -> Blob/API -> browser; authenticated range requests are absent. | Stream provider to Blob and serve authorized `206` range requests without API/browser whole-object copies. |
| Durable timeout depends on polling | Timeout termination occurs in status reads; the UI stops polling well before the configured deadline. | Put durable timers/budgets in orchestration and use push/adaptive status delivery. |
| Voice sessions are unlimited | Session duration defaults to zero and frame/transcript processing repeatedly parses/maps growing state. | Set duration/idle/frame caps, parse once, and batch transcript updates. |
| Background work is invisible to scaling | Ingestion and Durable activities run in 0.5 CPU/1 GiB API replicas with no queue/CPU scaling rule. | Separate workers and load-test one/three replicas before setting queue/CPU/memory scale rules. |
| Proxy telemetry is duplicated/high-volume | Successful traffic can reach ILogger, custom events, file logging, Container stdout, APIM, and Log Analytics without adaptive sampling. | Sample success, retain errors, downgrade normal completion, disable ACA file logging, and cap/measure telemetry. |
| Client bundle eagerly includes closed surfaces | Static graph from the chat page reached 58 local files/775,758 source bytes before framework dependencies. | Dynamically load Studio, Library, media, voice, imagery, and provider-specific auth; add a bundle budget. |
| Canonical toolbox omits citation discipline | The skill exists and appears in the example, while the live manifest binds `skills: []`. | Bind it or label it example-only; still implement server-side citation verification. |
| Portal fails narrow reflow | At 390/320 px sticky fixed-height navigation overlaps content; Requirements reached 587 px document width. | Add a mobile navigation layout and assert no page overflow/landmark intersection at supported widths and 200% zoom. |
| Portal service counts are wrong | Cards count all resources sharing an Azure type, so web/API/proxy each show `3 live` despite one matching resource. | Match each card's resource pattern/key, not only `azureType`, and test expected per-service counts. |
| Pre-auth skip link has no target | Auth providers replace the chat subtree, so the root `#main` target is absent on sign-in/loading screens. | Give every auth/loading/error screen one focusable `main#main`. |
| Hydrated 404 becomes the auth landing | Server HTML contains the custom 404, but the root auth gate replaces it for signed-out users. | Keep route-level error/not-found content outside or represented inside the auth gate. |
| Diagram/new-tab accessibility is incomplete | Five Mermaid diagrams lack accessible names; `_blank` links do not announce behavior. | Label diagrams via heading/title/description and announce or remove new-tab behavior. |
| OS dark preference is ignored by the app | The portal follows `prefers-color-scheme`; the app stays light until a stored/manual theme is selected. | Initialize an unset preference from the OS while retaining explicit user choice and contrast mode. |
| Builder/library mobile layouts are fragile | Agent/MCP builders keep fixed two-pane layouts and library action rows do not wrap. | Reuse the workflow mobile shell and collapse secondary actions into an accessible overflow. |
| Document-scope semantics conflict | Empty means no library context in chat but all ready documents in workflows. | Use the same explicit `all\|selected\|none` state everywhere. |

### P3 - low/maintenance

- Split oversized coordination modules: web `ChatApp.tsx`,
  `ConversationInspector.tsx`, `voiceLive.ts`, `WorkflowBuilder.tsx`, and
  `AdminDashboard.tsx`; API `routers/chat.py`, `routers/realtime.py`,
  `routers/library.py`, `config.py`, and `gateway/client.py`.
- Remove or justify unused exports and dead seams (`useVoiceRecorder`,
  `transcribeAudio`, `listTools`, `getMsalInstance`, `hasCitations`,
  `PREVIOUS_TOKEN`, `VOICE_PROVIDER_IDS`, and repository `update_session`).
- Consolidate duplicated artifact stores, entitlement error mapping, safe filename/
  fencing/sanitization helpers, byte formatting, `jsonOrThrow`, feature-flag parsing,
  and inspector/workflow disclosures.
- Resolve Bicep warnings instead of accepting a permanently noisy compile baseline.
- Correct stale comments in MCP secret storage, library capability, Entra clock skew,
  FastAPI version notes, and proxy port documentation.
- Make the proxy image's standalone listener match its advertised `EXPOSE 8080`;
  Bicep currently repairs the mismatch with `Port=8080`, while a plain image run
  listens on 8000.
- Add precise security-response targets and a confidential conduct-reporting channel.

## Written, provisioned, or generated but not served

| Surface | Actual status |
| --- | --- |
| Foundry A2A | Schemas/examples/scripts produce plans/commands; no application runtime invokes remote A2A agents. |
| Foundry routines | Validation/planning-only; the provisioning script intentionally has no create path. Schema wording and example toolbox references imply more. |
| Custom analyzer `config` | Accepted and persisted, but analyzer resolution does not provision or apply it. |
| Voice dictation hook | `useVoiceRecorder` and `transcribeAudio` have no production consumer. |
| Voice agent/tool preferences | Computed in `ChatApp`, not accepted/rendered by the settings panel. |
| Foundry safety annotations | Generated for every deployment but neither enforced nor consumed by the application. |
| Citation-discipline skill | Authored and included in the example manifest; the canonical live toolbox binds no skills. |
| Image background studio | Duplicates chat image generation and conflicts with the conversation-first product design. |
| Azure Monitor workspace | Provisioned without DCR/scrape wiring. |
| App Configuration | Provisioned unconditionally with no key-values; proxy use is optional. |
| Event Hubs | Standard namespace is unconditional while proxy Event Hub telemetry defaults off. |
| API Center identity/assets | Container/identity are IaC; asset registration remains manual and the identity has no demonstrated role use. |
| APIM official-MCP product | Exists even when the official-MCP runtime feature is disabled. |
| Numerous Bicep outputs | Not consumed by downstream IaC; either remove them or declare them stable operator contracts. |
| Manual operator scripts | Model availability, APIM compilation, Entra bootstrap, Foundry provisioning, migration, canary, teardown, and purge are not live-behavior CI. Some are intentionally manual; several need stronger behavioral tests. |
| Portal status/requirements/feature JavaScript | Generated or hand-maintained snapshots, not live runtime truth. |

## Code quality and comprehensibility

### What works

- The backend consistently threads authenticated ownership through repositories and
  parent-resource checks.
- Feature validation is broad and generally fails closed.
- MCP egress controls are stronger than average: HTTPS-only, all-address DNS checks,
  no redirects, connect-time pinning, and execution-time revalidation.
- Streaming terminal persistence is cancellation-aware and does not emit success before
  canonical write completion.
- The web's `apiFetch` boundary is consistent; browser code does not directly invent
  dev identity.
- Chat and Voice Live tests cover difficult races, stale attempts, truncation, focus,
  persistence, and lifecycle behavior.
- `AGENTS.md` gives both agents and humans explicit invariants, commands, and extension
  seams.
- API tests contain roughly as many lines as API source (37,832 versus 37,635), and
  repository route/link generators provide useful broad contract coverage.

### What impedes understanding

- Large orchestration modules combine policy, transport, persistence, UI state, and
  feature-specific work. Their comments are thoughtful, but comments cannot replace
  smaller state machines and bounded interfaces.
- Several documents carry full dependency incidents or live-environment history in the
  main execution path. That is useful evidence but poor everyday navigation.
- Similar helpers and stores have drifted into duplicated defects.
- "Fail open for optional context" and "fail closed for canonical/security state" are
  not represented by a shared error taxonomy, making broad catches expensive to audit.

The code is generally clean at the local-function level. It is not lean at the
coordination level. Static analysis found 180 API modules and 1,418 functions.
`routers/chat.py` is 1,966 lines; its main `chat()` path is approximately 1,234 lines
with estimated cyclomatic complexity 143. `validate_runtime()` is approximately 368
lines with estimated complexity 85. An expanded advisory Ruff pass reported 441 issues,
mostly exception-message style and complexity; these are maintainability signals, not
failures of the repository's configured Ruff gate.

## Documentation and dedicated-site assessment

The Markdown architecture, memory model, user guide, configuration reference,
contributor guide, and generated documentation catalog are unusually strong. Every
tracked Markdown file is either listed or deliberately excluded, so a new document
cannot silently miss the portal.

The weakness is not absence; it is conflicting truth:

- Architecture says durable workflows and Speech Voice Live are default-off in one
  section and enabled in another.
- Deployment/memory runbooks retain superseded flag fallback behavior.
- Telemetry says alerts are backlog while roadmap/feature docs say they are live.
- Portal architecture treats canonical memory as derived and canonical persistence as
  best-effort.
- Portal requirements name old Python/Node images and CI dependencies.
- Portal feature posture omits durable workflows and marks live priorities off.
- Foundry routine schema/examples imply a deployable routine that current docs correctly
  say cannot be created.
- Services counts every resource sharing an Azure type, so each of the web/API/proxy
  cards claims `3 live` although the inventory has one matching resource for each.
- The public status snapshot was three days old during testing; its headline said
  33 healthy while every row's Azure Resource Health availability was `Unknown`.

Use two explicit fields everywhere: **template default** and **observed environment
state**, with timestamp, revision, source, and verification method. Generate the latter
from deployment evidence rather than hand-maintaining JavaScript.

This audit is listed in `site/data/docs.manifest.json` and therefore appears on the
dedicated documentation site after its normal Pages publication.

The dedicated site itself is small, navigable, keyboard-friendly, dark-mode aware, and
usable in forced colors. Its main structural failure is narrow reflow: at 390 and
320 px, the wrapping links remain inside a 62 px sticky header and overlap the page;
Requirements expanded the document to 587 px. The architecture page also downloads
about 753 KB of Mermaid JavaScript, and its five generated diagrams have no accessible
name.

## Infrastructure reproducibility assessment

### Reproducible today

- Core Azure resource topology, models, regional deployment catalog, APIM routes,
  identities, most role assignments, diagnostics, Container Apps, Cosmos containers,
  Blob/Search/Postgres, and optional Durable Task resources.
- No literal credential, tenant GUID, or subscription GUID was found in tracked IaC.
- Catalog generators and schema/policy checks catch substantial drift.
- The production proxy image built and ran non-root with a working health endpoint.

### External/manual prerequisites

- Subscription role-assignment permissions.
- Resource-provider registration and model/Marketplace/quota availability.
- Two Entra application registrations and, for GitHub, OIDC federation.
- Real owner/publisher/budget/alert values.
- Custom-domain DNS and initial hostname bootstrap.
- Foundry Toolbox and API Center data-plane asset reconciliation.
- Web IQ credentials or verified managed-identity entitlement.
- Potential retry around the documented Cosmos vector-capability ordering.

### Production gaps

- No explicit production profile.
- Public dev-auth clean-room default.
- Foundry local keys and broad direct-model RBAC.
- Partial/unreachable private networking.
- Single-region/non-zonal application and data planes.
- Incomplete backup/restore drills outside Cosmos PITR.
- Mutable model upgrades and application images.
- No post-deploy data-plane proof or automatic application rollback.
- The proxy image advertises 8080 but defaults its worker listener to 8000; current
  Bicep supplies the compensating `Port=8080` environment value.

Someone else can reproduce the demo with the runbook and sufficient permissions, quota,
and patience. They cannot click one button and obtain the documented production posture
from a fresh tenant without manual identity, quota, domain, and data-plane work.

## UX and brand assessment

### Aligned

- Conversation remains central, with sidebar, transcript/composer, and contextual
  inspector.
- Narrow-screen sidebar/inspector drawers implement focus trapping, Escape, inert
  background, and focus return.
- Inspector and MCP surfaces expose effective/inherited state, risk, approval, scope,
  ownership, trust, and unknown values.
- Markdown disables raw HTML; authenticated artifacts/media are fetched as blobs.
- Base light/dark/high-contrast palettes and generated orange/blue/near-black assets are
  coherent and gated.
- Core chat drawers, reduced-motion CSS, skip navigation, focus traps/return, keyboard
  tabs/accordions, high contrast, and responsive composer work are strong foundations.
- Real-browser public routes had logical focus order, visible focus, useful landmarks,
  working portal skip navigation, usable forced colors, and no active reduced-motion
  animations.

### Misaligned

- Empty/error conflation and optimistic model state violate "one effective source of
  truth."
- Static slash commands advertise capabilities that can be unavailable.
- One-click destructive actions violate the otherwise careful governance posture.
- Hard-coded semantic colors bypass accessible themes.
- Hidden persisted Voice Live agent/tool preferences can affect a session despite having
  no current settings control.
- Empty document selection means **none** in chat but **all ready documents** in a
  workflow.
- MSAL initialization can remain on `Signing you in...` indefinitely, and failed model
  persistence leaves optimistic local state visible.
- Decorative gradients and a settings-based image generator conflict with the committed
  restrained, conversation-first design.
- Admin is a long mixed wall of usage, resources, security, governance, and operations;
  Voice settings expose expert controls without progressive disclosure.
- Agent/MCP builders and library action rows are not robust at narrow widths.
- Conversation/agent/workflow deletion has no confirmation, unlike library/MCP
  deletion.
- The pre-auth sign-in/loading UI has no `main#main`, so the global skip link has no
  target. Hydration replaces the otherwise-correct server-rendered 404 with the auth
  landing for signed-out users.
- Streaming screen-reader announcements, nested modal isolation, and JavaScript smooth
  scrolling need signed-in real-browser proof. The smooth scroll also ignores the
  reduced-motion preference.

## Responsible AI and outcome effectiveness

The system has unusually strong technical foundations: randomized untrusted-context
fencing, owner partitioning, execution-time tool authorization, bounded fan-out,
MCP DNS/IP/redirect controls, honest unknown usage, memory deletion fencing, and
activity summaries that avoid prompts, arguments, results, and chain of thought.

Those controls govern **who may act** and **how much may run**. They do not yet prove
that an answer is grounded, that citations support claims, that a workflow produced the
requested effect, that recalled memory is appropriate, or that severe model output is
interrupted. Important outcome gaps are:

- content filters annotate but never block;
- hostile retrieved content can influence preapproved outbound actions;
- citations and media references are not server-validated evidence;
- retrieval failure silently becomes ungrounded context;
- automatic memory lacks global consent and answer-level provenance;
- workflow success can be model prose without an effect receipt;
- explicit region/data-zone preferences can silently relax;
- model metadata does not encode or prove tool/vision/grounding capabilities;
- generated media lacks a complete authenticity/provenance/accessibility contract; and
- CI has no groundedness, bias, safety, prompt-injection, voice-quality, or task-success
  release gate.

The following are recommended engineering thresholds, not legal conclusions:

| Evaluation | Required evidence | Proposed release gate |
| --- | --- | --- |
| Grounded document QA | Frozen corpus, conflicting/stale versions, tables and media spans | >=95% supported claims; 100% citation IDs resolve; <=2% unsupported claims |
| Grounding outage | Blob/Search/embed/Web IQ faults and empty retrieval | 100% degradation disclosure; zero false claims of access |
| Citation integrity | Invented URLs/IDs, duplicate names, irrelevant/conflicting sources | Zero fabricated/misresolved IDs; validated claim-source relation |
| Prompt injection | Malicious document/web/memory/MCP content and canary data | Zero unauthorized tool calls or canary egress |
| Workflow/tool truth | Success, denial, timeout, partial result, and a model falsely saying `done` | Zero false-success outcomes; 100% required-capability preflight |
| Memory privacy | Secrets, health/finance/third-party facts, contradictions, poison, concurrent forget/write | Zero unapproved sensitive saves/cross-user recall; 100% used-memory disclosure |
| Model capability matrix | Every catalog model across stream/tools/structure/long context/region | Every advertised combination passes; unsupported combinations reject |
| Safety | Human-reviewed severe/benign/jailbreak cases across text/media/voice | Every agreed severe case blocks/escalates; zero unsafe tool use |
| Voice/inclusion | Consented data by accent, locale, age, noise, disability; Unicode/name cohorts | Zero dangerous semantic errors; no unexplained subgroup disparity |
| Accessibility | Keyboard, NVDA/JAWS/VoiceOver, 200/400% zoom, forced colors, reduced motion | Zero critical/serious violations; all core tasks complete |
| Privacy/telemetry | Canary secrets/PII through prompts, errors, tools, voice, config | Zero secret/PII leaks; declared deletion paths verified |
| Media provenance | Generated/uploaded media, download/playback and assistive alternatives | Every generated artifact labelled/provenance-linked; accessible alternative present |

Human escalation is required for the content-filter exception, data-residency semantics,
sensitive-memory policy, and any deliberate business-versus-safety tradeoff.

## Performance and scalability assessment

### What the hot paths actually do

- Plain chat can perform session/history reads, memory embedding/vector recall, document
  scans/embedding/Search, a non-streaming web/tool loop, proxy queueing, APIM/Foundry,
  Cosmos terminal writes, and implicit memory planning.
- An agent can add up to five supervisor calls, eight tools, and two delegated agents.
- A document upload can buffer 50 MB, poll Content Understanding in a process-local task,
  create up to 5,000 chunks, embed them, and write Search in batches.
- A workflow can run six sequential steps with up to three model calls each.
- Voice relays 100 ms base64 PCM frames while transcript persistence follows a separate
  Cosmos-heavy batch path.
- Admin opens eleven concurrent requests; seven independently scan the same usage
  window.

### Strong performance foundations

Normal gateway traffic shares a lifespan `httpx.AsyncClient`; API streaming is
incremental; Next's API route streams both directions; session updates use Cosmos patch
plus ETags; agent/workflow fan-out and RAG/tool payloads are capped; embedding ingestion
is batched; Search responses omit vectors; parsing guards pages/zip bombs and runs off
the event loop; APIM/proxy retries avoid nested multiplication; and the proxy queue,
TTL, circuit breaker, and pooled clients are bounded.

### Confirmed risks

The highest-risk multipliers are the seven 50,000-row admin scans, full-history
model/UI work, sequential and incompletely metered auxiliary model calls, process-local
50 MB upload tasks, N+1 voice-turn persistence, per-token React/Markdown/smooth-scroll
work, post-buffer MCP/media limits, polling-dependent durable timeouts, fragmented
Cosmos credential/client pools, eager feature bundles, and duplicated proxy telemetry.

The deployed shape is intentionally demo-sized: 0.5 CPU/1 GiB containers with at most
three replicas, one Uvicorn process, APIM Basic v2 capacity one, Search Basic 1x1,
single-region serverless Cosmos, and no queue/CPU scale signal for background work.
This is not inherently wrong, but it cannot support an enterprise performance claim
without explicit concurrency, availability, and cost targets.

### Proposed SLOs

| Surface | Proposed target |
| --- | --- |
| Warm plain-chat application TTFT excluding provider | p95 <=500 ms |
| End-to-end normal chat TTFT | p95 <=3 s |
| App/gateway SSE inter-chunk gap | p95 <=150 ms; no 4 KiB batching |
| History read at 200 messages | p95 <=250 ms |
| RAG preparation | p95 <=750 ms |
| Voice relay application latency | p95 <=50 ms one way |
| Persist 100 voice turns | p95 <=5 s |
| Upload acknowledgment | p95 <=2 s with bounded RSS |
| Durable schedule/status | p95 <=500/250 ms |
| Admin at 50,000 records | p95 <=3 s and <200 MiB incremental RSS |
| Availability/completion | >=99.9% availability; >=99.5% stream completion |
| Capacity | CPU <70%, memory <75%, proxy queue p95 <100 ms, Cosmos/Search throttles <1% |

Load tests should cover 2-2,000-message chats, 1-1,000 sessions/user, 200 documents/user,
1/10/50 MB uploads at 1/5/20 concurrency, 1,000-500,000 usage rows, hostile 20 MB MCP
responses, maximum six-step workflows, 5/30/60-minute voice at 1-100 concurrent
sessions, and random-seek media up to 200 MB. Run steady state, 10x burst, two-hour
soak, scale-out, replica termination, Cosmos/Search throttle, MCP timeout, and client
disconnect. Report RU, queue delay, RSS/GC/event-loop lag, render counts, model calls,
tokens, telemetry GB, and total cost per successful user outcome.

## Improvement sequence

### 0-24 hours: contain and restore trust

1. Fix proxy configuration redaction, rotate the ingress key, and investigate/purge
   affected event destinations.
2. Re-authorize or replace the annotation-only content-filter exception and record the
   accountable decision.
3. Block reserved chat parameters and add `store:false` to direct Code Interpreter.
4. Mark the MCP APIM key output secure.
5. Prevent ACL save before a successful read.
6. Relabel stale portal health, stop mapping `Unknown` to healthy, and correct service
   instance counts.
7. Correct teardown guidance so it integrates existing Cosmos PITR and adds Blob/Key
   Vault recovery before destructive disposal.

### 1-2 weeks: make production outcomes provable

1. Add production/demo deployment profiles and fail production closed.
2. Add post-deploy canaries, revision capture, and application rollback.
3. Build once and promote immutable, locked, scanned, signed image digests.
4. Generate portal runtime/feature/deployment evidence from authoritative sources.
5. Make UI loading/error/empty/unknown states explicit; repair auth/model/document/
   voice truth and protect irreversible deletion.
6. Add server-owned source IDs, grounding state, tool/effect receipts, and live
   approval for tainted external actions.
7. Enforce workflow-compatible tools on the server.
8. Consolidate admin usage scans; route/paginate Cosmos queries; cap chat input/output;
   and repair per-event SSE flushing.
9. Add durable document enrichment and durable artifact prerequisites.
10. Narrow Foundry/Key Vault/monitoring RBAC and disable local Foundry auth.
11. Repair portal mobile reflow and public app landmarks/404 behavior.

### 30-60 days: align architecture with the enterprise claim

1. Define SLO, RTO, RPO, expected concurrency, and budget; then decide which APIM/ACA/
   Cosmos/Search/storage redundancy is justified.
2. Complete a coherent private-network profile or remove the partially advertised one.
3. Rehearse restore, regional failure, and rollback procedures.
4. Split large chat/realtime/library/web state machines along durable boundaries.
5. Add browser E2E and accessibility coverage for auth, chat, sharing, workflows, voice,
   200% zoom, reduced motion, high contrast, and screen readers.
6. Add the versioned Responsible-AI/effectiveness and load suites defined above and
   retain their evidence per model/catalog/prompt release.
7. Separate background workers, stream/range large data, bound long sessions, and
   tune scale only from measured load.
8. Separate ADR/postmortem history from current operator and agent instructions.

## Exit criteria for a production-complete claim

- No secret value can enter logs, events, deployment outputs, or client errors.
- Harm/jailbreak handling has an accountable decision record and independently tested
  enforcement/escalation with explicit release thresholds.
- FastAPI rejects every client attempt to replace server-owned prompt/routing/storage/
  tool fields.
- Untrusted context cannot cause an unapproved external action or data egress.
- Every displayed citation resolves to server-recorded evidence, and retrieval
  degradation is explicit.
- Memory recall/write is consented and answer-level provenance/correction is visible.
- Workflow success requires verified capability/effect receipts, not model prose.
- Explicit region/residency constraints are exactly satisfied or rejected.
- Production profile cannot deploy dev auth, placeholders, missing alert recipients, or
  process-local durable features.
- CI promotes the exact tested image digest and blocks critical source/image findings.
- Post-deploy canaries prove web -> API -> proxy -> APIM -> Foundry plus enabled
  document/tool/voice paths, with rollback on failure.
- Canonical data has tested restore procedures with declared RPO/RTO.
- Portal current-state facts are generated, sourced, and freshness-bounded.
- UI never represents error/unknown as empty/zero/success and meets WCAG 2.2 AA in real
  browsers.
- The supported load envelope meets declared latency, memory, RU, completion, and
  cost-per-outcome SLOs through browser -> API -> proxy -> APIM.
- Accepted single-region/cost tradeoffs are tied to an explicit SLO rather than an
  implied enterprise reliability posture.

## Branch and workspace trace

The audit used one branch only:
`ian-t-adams-gpt56sol-2026-08-03-repo-audit`. No audit sub-branches or additional
worktrees were created. Pre-existing `main` and `code-cleanup-pass` worktrees were
left untouched.
