# Hard admission: source contract and activation boundary

**Source-only, default off. Not a deployed quota or an Azure bill cap.**
`AI4IA_HARD_QUOTA_ENABLED=false` preserves the numeric soft-ledger policy.
The API and preprovision validator refuse deployed hard-mode activation. Even
local Cosmos activation is refused. There is no bootstrap command, migration,
balance-creation endpoint, acknowledgement override, or automatic empty balance
for an existing owner.

The executable local adapter is an explicitly seeded **test fake**. It is neither
restart-durable nor safe across processes. The Cosmos adapter operates only on an
existing compatible coordination document and has no create/upsert path or
credential/activation factory. This is partial implementation of
[issue #433](https://github.com/ian-t-adams/AI4IA/issues/433), not issue closeout.

## Units and coverage

`rolling-dispatch-v1` uses the existing rolling 60-second, 24-hour and 30-day
durations, not calendar/fixed windows. Its request unit is **one application
operation dispatched to a gateway or service**. A streaming parameter fallback
is another dispatch. An MCP invocation includes its protocol handshake; it is
not a count of individual HTTP packets. Direct compute counts one application
Code Interpreter `run` POST, not every provider-internal code execution or billed
sandbox. These units are deliberately different from soft metering's top-level
usage rows; soft summaries are not the hard coordination balance.

All paths use the shared `admitted_dispatch` boundary. HTTP authentication binds
the internal owner, nested asyncio work inherits that scope, and a hard-enabled
client without an owner refuses before sending. Durable worker threads establish
their own persisted owner, but **hard durable execution is refused** until
per-operation replay identity and outcome recovery are implemented.

| Metered surface | Execution boundary | Current hard coverage |
| --- | --- | --- |
| Plain chat, agent loops, child agents, in-request workflow steps, summaries and memory extraction | `ModelGatewayClient._post` and `_stream_request` | Application dispatch attempts; token/dollar caps refused without a proven downstream-attempt envelope |
| Chat Completions, Responses, Claude SSE and non-streaming calls | The same gateway boundaries after provider adaptation | Every HTTP attempt is admitted separately, including the `stream_options` fallback |
| Memory/library embeddings | Gateway `embed` POST | Request only; token/dollar caps refused |
| Images, video job creation, Mistral OCR, REST transcription and synthesis | Gateway POST wrapper | Request only; corresponding token/dollar caps refused |
| Content Understanding binary and inline analysis | `ContentUnderstandingClient._post_document` | Request only; page/downstream-model bounds unavailable |
| Direct Code Interpreter execution | `CodeInterpreterClient._post`, compute surface | One request and one application compute attempt; token/dollar caps refused |
| Code Interpreter file uploads | The same POST wrapper, external-tool surface | Request only; not a sandbox execution |
| WebIQ search, browse and the other enabled endpoints | `WebSearchClient._call` around SDK/transport execution | Request only; unknown service/downstream meters refused under token/dollar caps |
| BYO and official MCP tool calls and resource reads, including skill loads | `_call_with` / `_read_resource_with`, including handshake | Request only; unknown remote meters refused under token/dollar caps |
| Azure OpenAI realtime and Speech Voice Live | Shared `run_relay` boundary before `connector.connect` | One application session-open attempt; no token/dollar bound for the live session |
| Durable workflow activities, including previously queued work | Worker `_execute_step`, before execution | Refused in hard mode; no dependence on an HTTP ContextVar surviving a thread or replay |

Status/content polling, analyzer metadata reads, MCP discovery/listing, auth,
cleanup deletes, Blob/Cosmos/Search operations and Azure infrastructure charges
are not new model/tool-execution reservations. In particular, cleanup remains
available rather than retaining chargeable files because an admission cap was
reached. These exclusions are another reason this is not an Azure spending cap.
Local, non-metered built-ins and canonical source reads remain available.

### Why catalog token limits are not enough

The gateway path can do more than one backend attempt.
`infra/policies/simplel7proxy_backend_32.xml` has an outer retry with `count="50"`,
an independently decremented `RetryCount`, temporary-error failover, and
`Return429` / `RequeueAllowed` logic that can hand work back to SimpleL7Proxy.
The app sees final usage, not proof of all earlier attempts.

No multiplier is inferred from that outer count. The shipping admission factory
supplies **no** downstream-attempt envelope, so a configured hard token or dollar
cap refuses the gateway operation, even when the model has a catalog context
window and a price. Media, realtime and remote tools likewise never become free
because their meters are absent.

The contract tests can supply an explicit deterministic transport envelope.
For that fixture, text bounds use the catalog context plus the effective maximum
output, and embeddings require a recorded per-input catalog context. Opaque
continuation, provider-hosted tools, multimodal input and multiple outputs have
no supported bound. Dollar bounds additionally require a versioned USD price
snapshot. Reservation and settlement use the shared pricing helper's ceiling;
settlement never reloads current rates. Final usage cannot refund earlier
unaccounted-for retry attempts.

The separate [workflow monetary contract](workflow-automation.md#per-run-monetary-source-contract)
uses those same versioned bounds for an immutable per-run USD application-meter
limit. It is not this rolling owner quota and cannot bootstrap an owner balance
or weaken the hard-quota durable/nonlocal activation refusal. Its source ledger,
approval quote or successful fixture does not establish the missing shipping
attempt proof. Unknown remote service meters are still refused under a cap.

### Bounded one-attempt source transport

`ai4ia-one-attempt-v1` adds a **default-absent source contract**, not a deployed
capability or permission to activate monetary enforcement. The app factory still
supplies no `GatewayCapabilityVerifier`; `ModelGatewayClient.attempt_capability`
is `None`. A settings Boolean, administrator acknowledgement, apparent version
header, final response counter, or the presence of the new files cannot change
that. Nonlocal hard-mode activation and local Cosmos activation remain refused.

The trusted server integration surface is `ai4ia_api.gateway.attempts`:

| Surface | Meaning and authority |
| --- | --- |
| `no_replay_scope(owner)` | Nested reduction-only requirement, with no HTTP field or environment selector. It must match the independently authenticated admission owner. It grants no model, tool, priority or quota permission. Without verified compatibility it refuses before sending. |
| `GatewayCapabilityVerifier.capability` and `verify(capability)` | An injected, independently trusted integration must verify exact deployed compatibility before admission. No shipping implementation exists. Constructing a well-formed `VerifiedGatewayCapability` is not verification. |
| `GatewayRouteBinding` | Typed required readback: exact APIM origin, versioned API resource ID/revision, three-operation inventory, distinct subscription resource ID and exact API-only scope, plus a transition-fenced evidence epoch. Structural validation is not deployed verification. |
| `VerifiedGatewayCapability` | The route binding plus exact HTTPS proxy base, immutable API/proxy image digests, effective APIM-policy digest, topology/configuration digest, catalog digest, version and short expiry (at most five minutes). All are bound again before egress; mismatches/expiry fail closed. |
| `current_attempt_envelope(surface, payload, deployment=..., target=..., owner=...)` | Available only inside an active prepared actual gateway request, before shared `admitted_dispatch` and workflow `before_dispatch`. Checks exact adapted/frozen payload, owner, surface, deployment and URL; a mismatch raises, never falls back. Pass this result to the existing shared coverage/pricing helpers. |
| `ModelGatewayClient.attempt_capability` / `attempt_capability_for(api)` | Read-only general/provider-aware availability for preflight/display, never a per-request grant or sufficient admission evidence. Both remain unavailable with staging off or the shipping absent verifier; unsupported provider APIs are refused. Controller-wide `AdmissionController.attempts` remains a deterministic-test seam. |

V1 supports synchronous and SSE **stateless plain-text** Chat Completions,
Responses and text-only embedding input lists. **Claude is explicitly unsupported
on this versioned route**, even though its ordinary adapter shares a generic
proxy-facing chat path. Both selected API and catalog-provider checks refuse it;
workflow preflight refuses before creating a capped run. Ordinary Claude,
including SSE cleanup in a different task, is unchanged. V1 also refuses
all tool declarations and tool/opaque reasoning continuations, provider-hosted
tools, async/background requests, stateful IDs, media, multimodal input, unknown
parameters and multiple outputs. Responses retention stays `store=false`.
The same scope on other metered clients does not invent coverage: without a
prepared gateway proof the shared dispatch boundary refuses, even without a
numeric limit.
Embeddings still require a catalog input context and priced text still requires
the shared immutable price snapshot. A one-attempt transport alone is not a
complete meter bound.

#### Versioned route staging and construction contract

`AI4IA_GATEWAY_ATTEMPTS_V1_STAGED=false` is an infrastructure staging switch,
**not a runtime selector or proof**. Separately approved staging would add six
conditional children to the **existing** APIM: one API, three operations, one
API policy and one API-scoped subscription. It also adds a secret and Host2
configuration to the existing proxy Container App and passes the staging posture
to FastAPI. False creates none of those children or proxy additions. No new
APIM/Foundry account, role assignment, model deployment, capacity, user balance,
bootstrap, live probe or activation is part of this source change.

| Method | API prefix | Exact operation |
| --- | --- | --- |
| POST | `ai4ia-attempts-v1` | `/openai/responses` |
| POST | `ai4ia-attempts-v1` | `/openai/deployments/{deployment}/chat/completions` |
| POST | `ai4ia-attempts-v1` | `/openai/deployments/{deployment}/embeddings` |

There is no wildcard operation or fallback into legacy `/openai`. Source and
compiled-ARM controls discover the existing `openai`, `openai/realtime`,
`openai/v1/realtime`, `code-interpreter` and `speech/voice-live/realtime` API
prefixes; none is a root wildcard or an ancestor of `ai4ia-attempts-v1`.
The normal model wildcard is a child of the **legacy `openai` API only**.

The generated versioned API validates its exact path/method/model and authenticated
subscription membership even when **both markers are absent**, then runs the
shared byte/HMAC check before catalog initialization. It deliberately omits
`<base/>` in **every section**: a guard before inheritance cannot bound an
inherited paid `send-request` or `forward-request`. This isolated API retains
the owned subscription authentication, catalog/residency, priority, circuit,
concurrency and provider-authentication chain. Ordinary policy inheritance is
unchanged. A future operation-level override, fragment change, incompatible
policy-enforcement requirement or unknown effective policy invalidates proof;
do not insert inheritance to make staging pass. See the official
[policy scope/inheritance contract](https://learn.microsoft.com/azure/api-management/api-management-howto-policies)
and [API-scoped subscription contract](https://learn.microsoft.com/azure/api-management/api-management-subscriptions).

Proxy Host2 uses the exact `/ai4ia-attempts-v1` prefix (not `/*`),
`stripprefix=false`, explicit `probe=/`, and the distinct API-only key.
The probe sentinel selects the non-probing host type; merely omitting `probe`
would inherit the production loader's legacy echo probe. Host1 retains its
existing `/openai/status` probe. Before its atomic
send claim, the proxy checks this host shape, the original exact path/query and
body/model binding; a missing bounded host cannot select the catch-all Host1/key.
No probes does not establish health. The dedicated subscription cannot invoke
legacy `openai`, and the mandatory API membership rejects legacy/all-API
subscriptions. FastAPI still holds only its existing opaque proxy ingress key.
It leaves the ordinary base URL unchanged and constructs a fixed versioned URL
only for a selected supported operation, **before** prepared proof and owner CAS.
Encoded/ambiguous paths, extra operations and query credentials refuse rather
than normalize into an ordinary paid request.

**A safe server capability constructor remains blocked on evidence issuance.**
`GatewayRouteBinding.validate()` checks a necessary structural contract, not a
live topology. A future trusted verifier must authenticate complete readback of
the API revision/operations/effective policy, API-only key membership and
non-colliding key use, all serving API/proxy images and exact routing/config,
catalog/meter compatibility, and non-replaying ingress. Its `evidence_epoch`
must be invalidated **before** any relevant transition, including in-flight
admission, rather than merely expiring after a stale read. No source hash,
operator Boolean, final ACK, nonce cache or TTL supplies this cutover/lease
authority. There is no shipping reader/issuer/invalidation authority or verifier,
so neither staging nor merging this source makes a finite cap runnable.

The API freezes at most 1 MiB of canonical JSON bytes before verifier/admission
awaits, and sends those exact bytes. A request-bound one-shot claim precedes
HTTP egress. Its dedicated HTTPX transport has zero retries, HTTP/1 only, no
redirects, inherited hooks/auth, environment proxy or shared-client defaults.
The Chat Completions `stream_options` fallback is suppressed only for this
selected mode; an empty/malformed reply, 400, timeout or cancellation is not
permission to try again.
Selected Responses requests preserve a valid explicit output maximum rather
than applying the ordinary 16,384-token floor. Malformed, Boolean, nonpositive
and over-catalog maxima refuse before egress. This is independent of
fresh-session/canary authority; normal group and run restrictions still see the
exact adapted maximum at the shared admission seam.

The proxy binds the internal selector only after successful existing inbound-key
authentication, **before** configurable header stripping and profile enrichment.
It rejects unknown/duplicate or forged downstream metadata. The claim verifies
the exact original body hash, model, method and path/query, refuses direct or
unkeyed backend hosts and all async/recovery shapes, then atomically consumes its
request-lifetime state before `SendAsync`. Neither per-loop nor lifetime counter
resets restore that claim. Requeue and DTO persistence/recovery reject selected
requests outright; there is no async replay identity to recover.

The proxy signs `version.nonce.bodySha256 + LF + POST + LF + pathAndQuery + LF +
model` using HMAC-SHA-256 and the **distinct versioned API-scoped subscription
key** (conditionally staged on the existing APIM).
The API-to-proxy selector is `x-ai4ia-attempt`; only the proxy supplies
`x-ai4ia-proxy-attempt`. APIM checks the signature against its authenticated
subscription keys and the original body bytes before catalog routing, then
removes this metadata before provider egress. The shared header logger redacts
these fields; the new scoped key stays in the proxy's Container App secret.
This is a per-request authenticated binding, not a distributed nonce cache or
permission to replay a captured request as a new operation.

The proxy uses a fresh `SocketsHttpHandler`/client for each selected request,
HTTP/1.1 exact, nonempty byte content, `Expect: 100-continue` disabled,
`Connection: close`, no cookies/proxy/credentials/preauthentication, and no
redirects or reused connections. The client remains alive through the response
body and is disposed with it. These constraints exclude .NET's version fallback,
authentication/redirect and pooled-connection replay paths; TCP retransmission
inside one connection is not another HTTP operation. The inspected
[.NET 10 HTTP/1 implementation](https://github.com/dotnet/runtime/blob/v10.0.0/src/libraries/System.Net.Http/src/System/Net/Http/SocketsHttpHandler/HttpConnection.cs)
also marks an ambiguous empty response non-retryable after a content-bearing
request without `Expect: 100-continue`. Loopback controls exercise the installed
runtime, including lost replies and redirects, rather than trusting loop counts.

APIM preserves ordinary priority/circuit/concurrency selection, but selected
requests get `RetryCount=1`, no requeue, a pre-forward retained claim and a
non-repeating outer retry condition. Every forward branch explicitly uses
HTTP/1, no redirects and no request-body replay buffering for the selected mode.
Empty success, temporary errors and on-error cannot restore retry/requeue
authority. See the official
[`forward-request` contract](https://learn.microsoft.com/azure/api-management/forward-request-policy).
The acknowledgement `x-ai4ia-attempt-ack` binds the version and nonce on replies;
missing/mismatched acknowledgements retain uncertainty and cannot trigger
fallback. **Acknowledgements detect incompatible replies; they cannot prove a
safe first dispatch to an old proxy or APIM policy.**

Before any future factory can supply the verifier, owner-reviewed integration
must establish all of the following outside user-controlled request data:

- Every serving API/proxy replica runs the exact reviewed immutable implementation,
  and the complete effective APIM policy (all scopes, fragments, API revision and
  runtime capabilities) implements the same version. A source hash alone is not
  evidence of serving code or Azure policy compilation.
- The exact proxy origin, APIM destination, catalog/model protocol and credential
  scopes agree. Inspect ingress intermediaries, transports, inherited policies,
  profile/config refresh and forwarding rules for any duplicate, automatic
  retry, redirect, fallback or async path. Unknown platform attempts refuse.
- Evidence is fresh and invalidated before a configuration, route, key-scope,
  catalog or deployment transition can escape its guarantee. The short expiry
  limits stale observations; it is not a substitute for a cutover/invalidation
  protocol or a lease on an otherwise mutable topology.
- The supported provider's input/output meters, catalog bounds and price version
  are compatible. Opaque provider-internal work is not priced merely because one
  external HTTP request was sent. Complete independent staging evidence is
  required before monetary enforcement; no paid probe or activation is included
  in source validation.

The offline .NET suite compiles the actual generated policy expressions with the
installed SDK compiler and projects their control flow into loopback HTTP sends.
It proves source behavior, not Azure's sandbox/compiler, inherited live policies,
APIM implementation internals or serving-replica coverage. Ordinary controls
prove a second actual request occurs, not that APIM necessarily chose another
region. Direct proxy host-failover controls separately exercise multiple hosts.
Unknown and cancelled calls retain their full original reservation; oversized or
inconsistent total usage is unknown rather than an accounting-construction error.

#### Compatibility matrices: source refusal is not runtime readiness

With the **shipping absent verifier**, every selected API call below refuses
before contacting the proxy: zero paid egress. The first matrix preserves the
**unsafe historical unversioned #476 counterfactual**, where a false verifier
could select the ordinary catalog route. A signature or reply header did not
make that older route incapable of paid work; do not treat its ACK as proof.

| Counterfactual combination | Source result without truthful deployed compatibility proof |
| --- | --- |
| New API + new proxy + new APIM, marker-preserving non-replaying path | One provider attempt per claimed application request; no automatic retry or recovery. This still requires the documented exact-deployment/topology proof. |
| New API + new proxy + old APIM | **Unsafe:** an old policy can ignore markers and retry. The missing ACK is detected only after possible paid attempts. Zero paid egress is not guaranteed by this route. |
| New API + old proxy + new APIM | Zero provider sends **if the API selector reaches new APIM**: the required proxy HMAC is missing. Not an unconditional guarantee if the old proxy/intermediary strips both fields or routes elsewhere. |
| Present unknown, malformed, incomplete or mismatching metadata at new proxy/APIM | Refused before the next protected hop/provider send. |
| Both fields stripped before new proxy binding, or between proxy and APIM | **Unsafe:** absence selects the ordinary route. Stripping after successful proxy binding cannot erase its in-memory claim or its final header stamping, but an intermediary stripping both stamped fields remains outside the source guarantee. |
| Retry using the same prepared API object or proxy request object after a lost reply | Zero additional sends: the consumed claim is not restored by counter reset, error, cancellation or requeue. |
| New HTTP operation reusing identical nonce/body after a lost reply | **Not globally deduplicated:** a new request has new local state and may pay again. There is no distributed nonce cache. |

The separate versioned boundary changes routing and credential membership, not
the meaning of an ACK:

| Versioned source combination or defect | Result before provider egress |
| --- | --- |
| New API + new proxy + correctly staged new API policy | At most one provider send for the claimed request; actual activation still requires the typed deployed/topology proof above. |
| New API + new or old proxy + pre-v1 APIM | No versioned API/operation exists: route refusal, zero provider sends. The new proxy also refuses a missing exact bounded host before forwarding. |
| New API + old proxy + staged new APIM | Missing valid proxy HMAC refuses. With a legacy-only key, API-scoped authentication refuses first. Neither can become ordinary work. |
| Both markers stripped, before proxy binding or between proxy and APIM | Mandatory versioned path membership refuses; zero provider sends. |
| Versioned prefix stripped but the bounded scoped key retained | The key cannot authorize legacy `openai`; zero provider sends. |
| Wrong method/path/model/nonce/body/signature, duplicate headers, unknown version or encoded/wildcard alias | Refused before protected dispatch; no ordinary-route error fallback. |
| Same prepared/request object after timeout, requeue, redirect, cancellation or lost ACK | The consumed request-lifetime claim permits no second send. |
| A new physical HTTP request repeating the nonce/body | **Still not globally deduplicated.** It has new local state and can send once again; two such requests can pay twice. |

These are bounded source projections, not claims about arbitrary intermediaries
that replace the route **and** credential, Azure policy compilation, serving
replicas or provider-internal retries. The loopback suite executes the generated
policy expressions, all three operation rewrites and the real proxy worker.
It pairs versioned refusal with the same fixture's ordinary two-send retry,
and tests a real inherited extra send that the versioned policy cannot enter.
The independent compiled-ARM controls bind the projected routing/key scopes
to actual conditional resources. None is production capability evidence.

The workflow's durable owner-CAS operation digest independently includes the
exact adapted payload and versioned target before egress. Accepted work settles
from persisted frozen Bounds after proof unbinding. That durable application
identity is **not** global deduplication of repeated proxy/APIM HTTP nonces;
unknown usage and lost acknowledgements retain the original hold.

## Reservation state machine

The store contains one immutable-payload entry per operation. State transitions
replace that entry; settlement does not add a second charge alongside it.

| State / event | Accounting and retry rule |
| --- | --- |
| Reserved | The full envelope consumes capacity. A 120-second lease limits the interval in which dispatch can be claimed. |
| Dispatch claim | An ETag CAS changes reserved to dispatched **before** egress. Only its winner may send. An already-claimed identity returns a conflict, never a second provider request. |
| Complete, fully known usage | Settle once to the proven quantity under the original price snapshot. Requests and compute attempts are never refunded by a zero token count. Known terminal charges age through rolling windows from settlement time. |
| Cancellation, timeout, missing/partial usage or ambiguous error after claim | Keep the full reservation as unknown. Unknown/dispatched entries remain charged in every applicable window and never expire automatically. |
| Lost coordination acknowledgement | Do not send on uncertainty. A committed dispatch claim remains non-replayable even if no application response was delivered. |
| Explicit release or abandoned reserved lease | Release only work whose state is still reserved. The same CAS fences a late dispatcher; an expired/released ticket cannot send. |
| Observed usage exceeds its envelope | Retain the actual quantity and block further admission pending reviewed reconciliation; do not hide the underestimate. |

Unknown holds can exhaust an owner's allowance indefinitely. There is deliberately
no automatic refund, force-clear API, or success-shaped fallback. Resolving them
requires a separately reviewed reconciliation protocol with trustworthy evidence.
Storage errors also prevent unlimited hard-mode owners from dispatching.

An operation identity contains a state epoch, store-issued timestamp and a hashed
server operation key. Its digest binds the owner, surface, frozen canonical
payload and original bounds. Reuse with a changed payload is rejected. Caller
mutation during a store await cannot change the JSON that is actually sent.
Retries of still-reserved work recheck current limits without counting themselves
twice. A completed operation is not a provider-response cache: retries cannot
resend it to recreate missing output.

Identities have a 30-day validity/replay horizon and a monotonic persisted floor.
Terminal pruning requires **both** that replay horizon and the longest meter
window to have expired. A pruned old key is rejected, not treated as new work.
Active and unknown entries are never pruned. The state has at most 1,024 entries
and a 512 KiB escaped JSON budget, whichever is reached first. Exhaustion refuses
new admission; it does not evict protected entries.
Admission also reserves worst-case timestamp, outcome, digest and charge growth
for **every** retained reserved/dispatched operation. Existing tickets recheck
this transition space before dispatch. A currently fitting reservation must not
consume the bytes an already-admitted operation needs to settle or expire safely.

## Cosmos isolation and evidence

The source adapter borrows the existing `usage` container, partition `/userId`.
Its fixed document id is `hard-quota-state-v1`, kind `hard_quota_state`, with an
explicit policy version and epoch. Every usage summary, record query, projected
admin rollup and session query excludes **both** the reserved id and kind while
retaining legacy usage rows. A coordination document cannot become an apparently
free usage row or poison an otherwise healthy usage query.

Persisted quota keys are required recursively before Pydantic construction
defaults can run, including explicit null unsupported axes/timestamps and
explicit `blocked`/`entries`. Missing state cannot mean zero consumption.
Persisted request/compute quantities must also match their dispatch surface.
Defaults remain available for normal in-memory model construction, not recovery
of an incomplete durable accounting document.

Key presence alone is not complete accounting. Persisted reads and the shared
snapshot/write validation reject phase-inconsistent evidence before reconciliation
or rolling-window aging. A known settled record requires `outcome=complete`, a
settlement identity, and known charges for every originally bounded token/dollar
axis; measured zero is valid and an originally unsupported axis may remain null.
Unknown records require an outcome and settlement identity but retain the full
bound, including when `outcome=complete` arrived without complete usage. They
never become known history merely because their held amounts are finite.
Reserved/dispatched records have no terminal evidence. Released records have no
dispatch, outcome or settlement identity and retain an explicit zero charge.
Dispatch timestamps must fall within the original inclusive reservation lease;
settlement cannot precede dispatch, but accepted work may settle after lease
expiry or the replay horizon. No malformed row is repaired, repriced or refunded.

The settlement digest is an idempotency identity, not recoverable raw usage.
It hashes the original settlement input before request/compute attempt counts
are normalized. Validation therefore does not reconstruct it from the retained
charge or infer the missing actual usage of an unknown record.

The adapter requires observed single-region writes, the exact owner partition,
non-expiring container retention, a compatible existing document, and an ETag.
It uses the Cosmos response `Date` for coordination time, with no replica-clock
fallback. Replacements use `IfNotModified`; 412 causes a bounded reread/retry.
Missing state or other storage failures never create a balance. Azure's
[OCC contract](https://learn.microsoft.com/azure/cosmos-db/database-transactions-optimistic-concurrency)
does not provide this guarantee across independently accepting multi-write regions.

Only hashes, quantities, fixed states and bounded version metadata enter quota
state. Prompts, tool arguments, URLs and credentials are not stored there.
Model-call receipts carry bounded `admissions` evidence; usage records carry up
to eight `hardQuota` entries plus `hardQuotaCount` for the owner execution scope.
A larger count explicitly means the list is incomplete. These quantities are
admission estimates, not another usage/cost total to add to existing telemetry.
Initial dispatched evidence survives a settlement failure, and the ordinary
32 KiB escaped receipt budget still applies. Historical reads never reprice.

## What must happen before activation

Owner approval is still required for the exact bootstrap, retention and rollout
protocol. Existing soft history may contain unknown or missing usage, so it
cannot be silently imported as a zero balance. A state marker or Boolean
acknowledgement alone is not evidence of that work.

The remaining boundary includes a proven gateway-attempt/meter envelope where
token/dollar enforcement is desired, durable per-operation identity/outcome
recovery, explicit reconciliation of unknown holds, validated account/clock
observations, appropriate bounded-state capacity, and a cutover of **all**
replicas/workers without older writers escaping admission. Only then can an
owner-reviewed change remove the unconditional deployed-activation refusal and
provide a real store factory. No such activation, migration, production write,
deployment or issue closure is performed by merging this source.
