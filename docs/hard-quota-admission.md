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
