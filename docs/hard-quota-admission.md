# Hard admission: source contract and activation boundary

**Default off. Not a deployed quota or an Azure bill cap.**
`AI4IA_HARD_QUOTA_ENABLED=false` preserves the numeric soft-ledger policy.
Enabling it outside the explicitly seeded local test fake requires Cosmos, Entra
outside local and `AI4IA_HARD_QUOTA_ROLLOUT_ID`, which selects exactly one
operator-authored [`hard_quota_rollout_v1` record](#control-record). API startup
refuses unless that record and the storage layout validate. The approved scope is
**request-count only**: token and USD caps stay refused. There is no migration,
balance-creation endpoint, acknowledgement override, automatic enrollment or
automatic empty balance for an existing owner.

The executable local adapter is an explicitly seeded **test fake**. It is neither
restart-durable nor safe across processes. The Cosmos adapter operates only on an
existing compatible coordination document and has no create/upsert path. Owner
documents come only from the [operator bootstrap](#operator-bootstrap), which is
dry-run by default and create-only. Merging this source bootstraps, approves,
activates or deploys nothing. This is partial implementation of
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
or weaken the hard-quota durable-execution and token/USD refusals. Its source ledger,
approval quote or successful fixture does not establish the missing shipping
attempt proof. Unknown remote service meters are still refused under a cap.

### Bounded one-attempt source transport

`ai4ia-one-attempt-v1` adds a **default-absent source contract**, not a deployed
capability or permission to activate monetary enforcement. The app factory still
supplies no `GatewayCapabilityVerifier`; `ModelGatewayClient.attempt_capability`
is `None`. A settings Boolean, administrator acknowledgement, apparent version
header, final response counter, or the presence of the new files cannot change
that. The approved request-count rollout scope refuses token/USD caps and bounds
regardless, so no rollout record can activate monetary hard admission.

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
| Cancellation, timeout, missing/partial usage or ambiguous error after claim | Keep the full reservation as unknown. Unknown/dispatched entries remain charged in every applicable window and never expire automatically. Exception: under the approved [request-count scope](#request-count-scope), a request-only record has no bounded token/USD axis, so any terminal outcome settles its exact attempt counts as known history (below). |
| Lost coordination acknowledgement | Do not send on uncertainty. A committed dispatch claim remains non-replayable even if no application response was delivered. |
| Explicit release or abandoned reserved lease | Release only work whose state is still reserved. The same CAS fences a late dispatcher; an expired/released ticket cannot send. |
| Observed usage exceeds its envelope | Retain the actual quantity and block further admission pending reviewed reconciliation; do not hide the underestimate. |

Unknown holds can exhaust an owner's allowance indefinitely. There is deliberately
no automatic refund, force-clear API, or success-shaped fallback. The only
resolution path is the evidence-bound, digest-approved
[operator hold resolution](#unknown-hold-resolution), and only for request-only
`dispatched` holds. Storage errors also prevent unlimited hard-mode owners from
dispatching.

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
Active and unknown entries are never pruned. The approved request-count scope
shortens both bounds, as described [below](#request-count-scope). The state has
at most 1,024 entries and a 512 KiB escaped JSON budget, whichever is reached
first. Exhaustion refuses new admission; it does not evict protected entries.
Admission also reserves worst-case timestamp, outcome, digest and charge growth
for **every** retained reserved/dispatched operation. Existing tickets recheck
this transition space before dispatch. A currently fitting reservation must not
consume the bytes an already-admitted operation needs to settle or expire safely.

## Cosmos isolation and evidence

The source adapter borrows the existing `usage` container, partition `/userId`.
Its fixed document id is `hard-quota-state-v1`, kind `hard_quota_state`, with an
explicit policy version and epoch. Every usage summary, record query, projected
admin rollup and session query excludes **both** the reserved id and kind while
retaining legacy usage rows. They also exclude the `__ai4ia_hard_quota_control__`
partition and kind `hard_quota_rollout_v1`, each independently, so a damaged or
misfiled rollout record is still not usage. A coordination document cannot become
an apparently free usage row or poison an otherwise healthy usage query.

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
The one other known shape is a request-only attempt settlement: no bounded
token/dollar axis, any terminal outcome, and a charge exactly equal to the frozen
attempt bound. Only the request-count scope and the operator resolution write it.
Unknown records require an outcome and settlement identity but retain the full
bound, including when `outcome=complete` arrived without complete usage. They
never become known history merely because their held amounts are finite.
Reserved/dispatched records have no terminal evidence. Released records have no
dispatch, outcome or settlement identity and retain an explicit zero charge.
Dispatch timestamps must fall within the original inclusive reservation lease;
settlement cannot precede dispatch, but accepted work may settle after lease
expiry or the replay horizon. No malformed row is repaired, repriced or refunded.

A retained known settlement above its corresponding non-null frozen token or
microUSD bound requires `blocked=true`. Settlement and state validation share
the same record classifier. Fresh construction, persisted reads and shared
snapshot/write validation reject a contradictory explicit `blocked=false`
before reconciliation or pruning, including copies made with `model_copy`.
Equality is allowed, a zero bound is enforced, and an unsupported axis is not an
implicit zero bound. Request-only measurements do not invent token/dollar bounds.
The historical block survives terminal pruning; valid settlement of already
dispatched work remains available on a blocked owner. This rejects contradictory
retained evidence, not unrecorded corruption: it cannot detect a past violation
whose evidence and block have both already been removed.

The settlement digest is an idempotency identity, not recoverable raw usage.
It hashes the original settlement input before request/compute attempt counts
are normalized. Validation therefore does not reconstruct it from the retained
charge or infer the missing actual usage of an unknown record.

The adapter requires observed single-region writes, Session consistency, the exact
owner partition, non-expiring container retention with analytical storage off, a
compatible existing document, and an ETag. Account and container metadata have
separate service limits and no SLA, so the production factory validates them at
startup and then at most every 60 seconds; a failed observation is never cached.
The adapter's default of zero keeps per-operation validation for the conformance
fakes. It uses the Cosmos response `Date` for coordination time, with no
replica-clock fallback. Replacements use `IfNotModified`; 412 causes a bounded
reread/retry. Missing state or other storage failures never create a balance: an
absent owner document is refused as requiring the reviewed bootstrap. Azure's
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

## Request-count activation contract

This is the complete source path from default-off to an approved deployment. It
is **deployable only behind owner-approved evidence**; the application checks the
shape and ordering of that evidence, not its truth. Production runs the API as a
single-revision Container App (one to three replicas) with the durable worker
inside it, and all metered egress leaves through the API process.

### Control record

Outside the explicitly seeded local fake, `AI4IA_HARD_QUOTA_ENABLED=true` also
requires the Cosmos store, Entra outside local and an
`AI4IA_HARD_QUOTA_ROLLOUT_ID` matching `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.
Startup reads exactly one item, in the existing `usage` container, partition
`__ai4ia_hard_quota_control__`, with `id` equal to that setting. No UUID owner id
can equal that partition. The strict schema forbids extra fields (Cosmos `_*`
system properties are ignored):

```json
{
  "id": "<approved-rollout-id>",
  "userId": "__ai4ia_hard_quota_control__",
  "kind": "hard_quota_rollout_v1",
  "protocol": 1,
  "state": "approved",
  "policyVersion": "rolling-dispatch-v1",
  "scope": "request_count_only",
  "singleWriteRegion": true,
  "noCoordinationExpiry": true,
  "coverageStart": 1790000000,
  "writerCutoverEvidence": "https://<review>/writer-drain",
  "bootstrapEvidence": "https://<review>/bootstrap",
  "recoveryRetentionEvidence": "https://<review>/recovery-retention"
}
```

This is a schema illustration, **not** an approval to create it. There is no API,
Bicep data write, startup repair or tool that authors this record. Markers must be
the exact JSON values (`true` is not `1`). `coverageStart` is a strict integer of
coordination-store seconds and must not be later than the `Date` of the startup
read: an approval cannot precede the cutover it attests. Each evidence value is a
printable-ASCII `https` URL of at most 1,000 characters with a host and no
userinfo, query, fragment or non-443 port. References are never fetched.

Startup then requires the account to have exactly one writable location, no
multi-write and `Session` consistency (the IaC setting), and the `usage`
container to be `/userId` Hash, with no default TTL and analytical storage off. Only
after the record and layout validate does the factory construct the Cosmos store
and its request-count scope. Any defect, missing `Date` or a 30-second startup
timeout refuses API startup with a fixed message. There is no fallback to soft
admission, a local store or an empty balance.

### Seeding rule

Let `H = max(document validAfter, rollout coverageStart)`, in coordination-store
seconds. An owner document is authoritative only for dispatches admitted at or
after `H`. For each configured request-count cap with rolling duration `W`:
`requestsPerMinute` covers every surface with `W` = 60 seconds, and
`computeExecutionsPerDay` covers compute operations with `W` = 24 hours. An
admission at store time `t` is refused (429) while `t - W < H`. The uncovered part
of the window counts as already consumed. From `t >= H + W`, the ordinary sum over
retained entries applies. Uncapped dimensions are unaffected: an unlimited owner
dispatches immediately and every dispatch is still recorded.

Soft usage rows are **not** imported as counts. They can prove "at least", never
"at most". A soft row is one top-level turn that may contain many application
dispatches: agent loops, MCP/WebIQ/tool calls, embeddings, memory and summaries.
Several dispatch surfaces write no soft row, and soft writes are best-effort:
failed writes, crashed replicas and cancelled streams leave nothing. The fence
already treats the whole uncovered window as consumed, so no soft count could make
the seed safer. Using one to admit earlier would be unsound. `coverageStart`
covers non-enforcing writers that ran after an early bootstrap. `validAfter`
covers late enrollment and recreation of a deleted document: deleting a document
is never a reset.

### Request-count scope

Under the approved scope, and only there:

- Any configured `tokensPerDay`, `tokensPerMonth`, `costPerDayMicroUsd` or
  `costPerMonthMicroUsd` refuses (503) before any history is read, and any bound
  with a token or microUSD axis is refused. Owners with such caps are fully
  refused; remove the caps or keep those owners in soft mode.
- A request-only record's enforced quantities are its attempt counts. The one-shot
  dispatch CAS fixes them, and every send of the operation precedes its terminal
  settlement. Downstream proxy/APIM retries are not application dispatches. Any
  terminal outcome (`complete`, `cancelled`, `timeout`, `error`, `unknown`)
  therefore settles `requests=1` (and `compute=1` for a sandbox) as known history
  that ages from settlement. A `dispatched` record whose settlement never landed
  stays held forever.
- Identities are issued at each claim's own store time, and the replay horizon is
  300 seconds. Terminal entries are pruned once their key has expired and the
  longest window that can count them has passed: 60 seconds after settlement, or
  24 hours for a settled compute attempt.
- Capacity is still the 512 KiB document. At about 873 bytes per settled
  request-only entry, an owner can hold roughly 600 recent operations. Holds,
  in-flight work and a day of compute attempts count toward that. RU cost grows
  with document size: Cosmos charges about 5.5 RU per KiB for an unindexed insert
  and twice that for a replace. Each dispatch replaces the document three times.
  Measure latency and RU at representative sizes before activation.

A future token/USD scope needs its own rollout record and a new `coverageStart`.
Request-count retention does not preserve longer-window history, and the fence
makes that safe.

### Operator bootstrap

`python -m ai4ia_api.hard_quota.operator bootstrap` runs from the API development
environment or inside the API image with its existing managed identity. The
module lives at `app/api/src/ai4ia_api/hard_quota/operator.py`. The tool needs an
identity with Cosmos data-plane access. Choosing it is an activation decision;
this source grants no role.

```powershell
python -m ai4ia_api.hard_quota.operator bootstrap `
  --endpoint https://<account>.documents.azure.com/ --database ai4ia `
  --cohort .\private-cohort.json --output .\bootstrap-plan.json
python -m ai4ia_api.hard_quota.operator bootstrap `
  --endpoint https://<account>.documents.azure.com/ --database ai4ia `
  --cohort .\private-cohort.json --apply --approve-plan <plan_sha256> --output .\bootstrap-apply.json
```

The cohort is explicit: at most 256 canonical UUID internal owner ids, via
repeated `--owner` or a private `{"schemaVersion": 1, "owners": [...]}` file. It
has no duplicates and no control partitions. Include the deploy-canary identity:
an owner without a document is refused, and post-deployment verification would
fail and roll back. The default run is read-only. It validates the same layout as
startup, then point-reads each owner: absent documents plan `create`, valid
documents `keep` (never touched), and invalid documents `blocked` (exit 2, never
repaired). The plan digest binds the tool/contract source hash, cohort, endpoint
host, database, layout and each owner's observation, which is absence or the
immutable epoch and `validAfter`.

`--apply` and `--approve-plan` are valid only together. A fresh observation must
reproduce the approved digest exactly, or nothing is written. Each absent owner then
gets `create_item` only, with no write retry. The document has no entries,
`blocked=false`, a new epoch, and `validAfter = replayFloor = observedAt` set to
the store `Date`. A concurrent document, timeout or ambiguous acknowledgement
stops the run as partial or unknown (exit 2). There is no upsert, replace,
delete, backdating or retry. Created documents are read back through the
runtime's strict decoder. Output files must be new. Reports contain owner hashes,
never raw ids. Owners added later are bootstrapped by the same approved procedure;
their creation time fences them.

### Unknown-hold resolution

Under the scope, the only holds are `dispatched` request-only records whose
settlement never landed. Causes are a crash, a scale-in or deployment kill, a lost
settlement acknowledgement or a store outage. They count in every window and are
never pruned.

```powershell
python -m ai4ia_api.hard_quota.operator resolve `
  --endpoint https://<account>.documents.azure.com/ --database ai4ia --cohort .\private-cohort.json `
  --dispatched-before <epoch-seconds> --evidence-reference https://<review>/replicas-terminated
```

- **Who:** an operator with Cosmos data-plane write authority. The owner first
  approves the exact plan digest, and the operator reruns with `--apply
  --approve-plan`. There is no API route, admin UI, automatic sweep or runtime
  expiry.
- **Evidence:** the https reference must show that every API replica alive at
  `--dispatched-before` has terminated. The reference is hashed into the plan and
  into each resolved record's settlement digest. The cutoff must also be at least
  24 hours before the store clock, as a mechanical margin.
- **Scope:** only `dispatched`, request-only records dispatched before the cutoff,
  on an unblocked owner. Younger holds, `unknown` records, token/USD-bounded holds
  and blocked owners are listed but not resolvable.
- **Transition:** each planned record is re-verified byte-for-byte on a fresh read.
  It becomes `settled` with `outcome="unknown"`, `settledAt` = the store time and
  the full frozen bound as the charge. The charge therefore stays in every window
  for `W` after resolution, strictly after the proven dispatch, never below proven
  usage. Other entries are untouched. A 412 rereads and re-verifies, at most eight
  times. A planned record that changed meanwhile stops the run. A late genuine
  settlement then fails as `settlement changed` (409), never a double count.

Durable per-operation replay identity remains unimplemented, so hard durable
execution and workflow automation stay refused.

### All-writer cutover and rollback

Non-enforcing writers are any API revision without hard mode and its in-process
durable worker. They record no reservations, so the runtime cannot detect them.
The protocol makes their period uncoverable instead:

1. Deploy this source with hard mode off. Bootstrap the cohort, before or during
   the change window.
2. Drain: reach a fresh observation, at `T_drain`, where no replica of any
   non-enforcing API revision is running. This is a maintenance window: the app
   serves nothing from the drain until the enforcing revision is ready.
3. Author the control record with `coverageStart >= T_drain` and the three
   evidence references, after owner approval.
4. Deploy the exact signed release with `AI4IA_HARD_QUOTA_ENABLED=true` and the
   rollout id through the approved release path.

`writerCutoverEvidence` must show the drain observation, `coverageStart >=
T_drain` and the exact release digest to be activated. `bootstrapEvidence`
references the approved plan and apply reports, including the deploy-canary
identity. `recoveryRetentionEvidence` covers the no-TTL container, continuous
backup and point-in-time restore, the hold-resolution owner and the rollback rule
below. A Boolean, elapsed wait or passing test is not evidence. The drain
mechanics under the deployment workflow's capture/rollback path need rehearsal
before approval. Zero-downtime cutover would need a separate record-only design.

**Any non-enforcing writer after `coverageStart` ends the rollout.** That includes
turning the flag off, a rollback to an older or soft revision (including a failed
activation's automatic rollback), and a restore or failover of the account.
Re-enabling requires a new rollout id whose `coverageStart` follows a new drain.
Existing owner documents may be reused; the fence covers the gap. Never delete an
owner document to reset it. Continuous canary endpoints remain unavailable in hard
mode.

## What must happen before activation

Merging this source performs no bootstrap, record authoring, drain, activation,
production write, deployment or issue closure. Before the owner authors a control
record, they need concrete evidence of:

- a rehearsed drain and an observed `T_drain`
- the exact signed release that will be activated
- an approved bootstrap plan and apply readback for the cohort, including the
  deploy canary
- measured RU and latency for representative document sizes
- a reviewed recovery/retention policy and hold-resolution owner
- the operator identity that will run the tool

Token/USD enforcement additionally needs the proven gateway-attempt/meter envelope
and its own rollout. Durable per-operation replay identity/outcome recovery,
zero-downtime cutover and state compaction also remain outstanding.
