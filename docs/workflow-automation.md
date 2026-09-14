# Resumable workflow automation

Workflow automation adds exact-call approval suspension and finite safe-only
schedules to the existing FastAPI-hosted Durable Task worker. Both features are
default off. They do not create another model path, provision another scheduler,
authorize a workflow permanently, or enforce an Azure dollar cap.

## Enablement and scope

| Setting | Default | Prerequisites |
| --- | --- | --- |
| `AI4IA_WORKFLOW_APPROVALS_ENABLED` | `false` | Existing durable host, metering, finite runtime and protocol-v1 session readiness |
| `AI4IA_WORKFLOW_SCHEDULING_ENABLED` | `false` | Resumable approvals and the same durable host |

Outside local, Entra and Cosmos are required. The existing
[conversation-deletion rollout](runbooks/conversation-deletion.md) must be
approved and ready for newly created conversations. An old conversation is
refused, not silently enrolled. This dependency is not permission to create a
rollout record, modify existing data, grant roles, or deploy.

The infrastructure forwards both flags explicitly, including `false`.
Availability comes from `/api/workflows/automation/config`, not a browser flag.
No cron service, email service, new identity or permission is introduced. Model
requests retain the FastAPI -> SimpleL7Proxy -> APIM route.

Owned private workflows independent of group-derived authority can run
unattended. A persisted JWT, email, login record or previously resolved group
membership is not authority for a timer. Published/group-dependent execution is
refused when a current interactive Entra consumer is required; it is never
silently substituted with a private definition or a publisher's data.

## Run and approve

In the existing workflow builder, select a saved workflow and model, then use
**Resumable runs and safe schedules**. Choose input, finite runtime/output/request
bounds, and explicitly select execution **without a hard dollar cap**.
The separate **USD application-meter maximum** choice is unavailable unless the
server has a verified bounded transport. The shipping factory currently has no
such proof; enabling the workflow flags does not make this choice available.

The run captures the owned source revision/digest or exact reviewed source,
resolved agents, effective tool contracts, resources and model deployment.
Existing synchronous runs and earlier durable histories keep their separate
execution contracts. Session/run auto-approval is not inherited.

When an otherwise executable call needs approval:

1. The runtime persists that exact call and its continuation before executing it.
   Already completed model/tool work, usage and receipt evidence are retained.
2. **Workflow approvals** shows an owner-scoped pending item independently of the
   original request or browser tab.
3. **Review exact call** shows the complete safe argument JSON, destination, risk,
   source identity, immutable spend evidence and expiry. It issues a short-lived normal one-time grant;
   only the grant hash is stored. Reloading requires a fresh review, not
   recovering a bearer secret from storage.
4. **Approve this call and resume** consumes that grant by conditional write.
   The same orchestration continues the stored operation after current
   authorization checks; it does not ask a model to recreate previous work.
5. **Deny and stop**, expiry, cancellation, source revocation or unsupported
   context changes prevent further dispatch. There is no implicit skip policy.

A request that cannot be displayed and bound accurately is not approvable.
Duplicate JSON keys, non-finite values, oversized/truncated arguments and
execution-significant credential masking are refused. Credentials remain in
existing execution-time secret resolution, not approvals or notifications.

An approval is bound to owner, conversation generation, source/run, operation,
tool/schema/scopes, destination, exact argument digest and expiry. A review in
another tab can invalidate an older challenge. A grant cannot authorize a new
run, changed arguments or a later repetition of the same tool call.

New drafts also bind an immutable spend quote to that exact operation, the
run's source/limit fingerprint and the current monetary-account revision.
The quote records USD, a conservative amount or typed unknown, coverage/basis,
versioned prices and the attempt contract when applicable, and the exact
remaining run budget. The one-time challenge binds the quote digest as well as
the stored arguments. A changed quote, budget, schema or destination cannot use
an older challenge. Quotes grant neither money nor execution permission.

Ordinary reads never reprice a historical quote. **Refresh spend quote** is an
explicit new review of the same stored call: it rotates the challenge, does not
extend the original expiry, and does not repeat accepted model/tool work.
Old v3 drafts without spend fields remain unquoted and cost-unknown; they do not
acquire invented zero balances on read. The older text-only review response
remains alongside the validated structured spend DTO for rolling web/API
compatibility.

An operation quote covers **only that exact tool operation**, not model work
after it. Remote MCP, WebIQ, Content Understanding, media, voice and other
unbounded service effects remain unknown and are not approvable under a finite
cap. Only a matching repository-owned local handler plus an execution-time
no-metered-effects guard can establish zero; a risk label or tool hint cannot.
The current finite profile does not admit tools, including those local tools.

## Calendar contract

Schedules support once, daily and weekly rules with explicit IANA timezone data.
The scheduler reads the operating system's TZif database through Python's
standard-library `zoneinfo` paths (or the operator's absolute `PYTHONTZPATH`
directories). It hashes the same bounded bytes supplied to `ZoneInfo.from_file`;
it does not import an optional Python timezone package or keep stale cached
rules. Links outside the trusted data root are refused. `system-tzif` labels
the rules source, while the persisted SHA-256 identifies the exact rules.

The shipping Python 3.12 image is checked for usable system IANA data by the
existing image smoke test. Scheduling startup requires `UTC`; each requested
zone must also exist. With scheduling disabled no timezone data is required.
Windows operators must install an IANA TZif directory and set `PYTHONTZPATH`
to its absolute path before starting the API or running calendar tests. For
example, `$env:PYTHONTZPATH = 'C:\iana-zoneinfo'` selects a directory containing
`UTC`, `America\New_York`, and the other supported region files. This is an
operator-owned data location, not a new Azure resource or authority grant.
Missing or invalid data is a startup/admission error, never a UTC-only fallback.

Their actual safe surface is rechecked at creation, occurrence admission and
execution. Unknown metadata, unavailable selected tools, recursive steps,
chat-only attachment-producing tools and unsafe declared tools refuse admission.
Known ambient web capabilities are excluded from the explicitly reviewed
safe-only mode. Non-bookkeeping mutations are also prohibited.

| Concern | Behavior |
| --- | --- |
| Once | Future local date/time; a nonexistent time is rejected |
| Spring gap | Daily/weekly nonexistent local slots are skipped, not shifted |
| Fall fold | Only the first occurrence of a repeated local time runs |
| Delay | A due slot has five minutes of grace; older slots are missed without backfill |
| Overlap | An active or unresolved run of that workflow blocks another occurrence |
| Recurrence | Wall-clock cadence, not completion-time-plus-24-hours drift |
| Timezone changes | A changed timezone ruleset pauses for review |
| Disable | Prevents future occurrence admission; an already admitted run has its own stop control |
| Edit | Creates a new immutable schedule generation; old timer events cannot launch it |

Schedules have at most 366 occurrences and at most ten registered schedules per
owner. Completed/disabled schedules retain identity rather than making old
delivery keys reusable. The recent history is bounded to twenty pointers.
The existing worker uses durable timers and bounded `continue_as_new`, not
HTTP polling as its trigger source.

## Bounds are not a bill guarantee

The default run limits are 64 application dispatches, 1,024 output tokens per
model call, and at most the existing 1,800-second operator runtime. A workflow
also retains six steps, at most eighteen model calls and forty-eight tool calls.
Lower caller limits are supported. Application dispatch accounting includes
nested metered work such as memory embeddings; it does not count Cosmos
bookkeeping, infrastructure charges or invisible APIM/proxy retries as known
model tokens.

Finite USD caps are refused under the current unproven downstream-attempt
envelope. The [hard-quota durable refusal](hard-quota-admission.md) remains in
force. Null, unpriced or incomplete usage never becomes a guaranteed zero or
enforced dollar limit. Already observed rates and effective model parameters are
retained; reads and retries do not reprice history.

### Per-run monetary source contract

`spendMode="usd_app_meter"` with an integer `maxSpendMicroUsd` expresses a USD
application-meter maximum for **one run**, not an owner/group balance or Azure
infrastructure bill cap. `1_000_000` micro-USD is USD 1. The limit is immutable
after admission; changing a schedule creates a new generation for future runs.
The normal `no_hard_dollar_cap` mode requires a null/absent monetary limit.

The first bounded profile is deliberately limited to stateless text with no
tools, automatic memory, selected documents, opaque continuation or
provider-hosted operations. Both request reductions (`allowTools=false`,
`allowAutomaticMemory=false`) must be explicit. A selected agent/step with tool
requirements is rejected, not narrowed behind the user's back. Uncapped runs
retain their ordinary tools, memory and exact-call approvals. Metadata, a local
test transport or an acknowledgment flag is not the required shipping
request-level attempt proof.

The source ledger is in the existing owner/run/effect records:

| Event | Monetary behavior |
| --- | --- |
| Actual protected dispatch | One owner ETag CAS reserves the conservative shared catalog/price bound before egress |
| Concurrent reservations | The same run balance is re-read on CAS contention; combined holds cannot spend the same remaining amount |
| Complete consistent usage | Settle with the original frozen rates and one-attempt contract; unused reservation can be released |
| Missing/partial usage, cancellation or lost outcome | Keep the full charged reservation, including across deadlines, restart and conversation cleanup |
| Replayed identity or changed payload | No second reservation or dispatch; an identical settlement acknowledgment does not charge twice |
| Delivered effect compaction | Retain cumulative settled money and reservation counts in the run account; unknown liabilities are not pruned |
| Observed bound violation | Retain the observed amount and block more work; do not relabel a failed assumption as a working cap |
| Accounting after owner stop/expiry | Continue known-work accounting without reconstructing execution permission |

Escaped JSON capacity, including SDK separators and future settlement fields,
is reserved for every admitted monetary transition. Incomplete money records do
not deserialize as empty accounts. The existing thirty-day replay floor permits
retiring a fully settled, delivered, inactive run; it never retires unresolved
money. Selection and validation use the same invocation-key timestamp, not a
later run creation time. A valid unrelated admission is not blocked by an
ineligible historical row. New per-run accounting does not enroll old histories, bootstrap an owner
hard balance, or relax the separate hard-quota durable/nonlocal refusals.

This ledger and quote engine do not by themselves complete all monetary
coverage. A verified shipping attempt integration is still required, and
quantified remote-tool/service bounds remain unsupported. The source contract
does not claim that merging it activates a monetary cap.

## Recovery and uncertainty

Run and schedule start requests use a timestamped idempotency key. Retries must
reuse the same complete request; changing source, input, model, resources or
limits is a conflict. The key's recovery horizon is thirty days, fenced by a
monotonic retained floor, not unbounded history.

An unconfirmed scheduler acknowledgment remains `acceptance_unknown`; it is
not a successful new run or permission to choose a different instance ID.
Approval events contain only wake metadata. Durable reconciliation also reads
recorded decisions, covering a crash between decision commit and event delivery.

A crash after remote acceptance but before a durable result is genuinely
unknown. The application cannot promise atomic exactly-once execution at a
remote provider. It retains that uncertainty and does not resend the model/tool
request to reconstruct an answer. Dispatched/unknown work does not expire into
free overlap or request capacity.

Cancellation can race already in-flight work. The stop prevents future dispatch;
late results may still add execution/usage evidence without reviving authority.
Conversation deletion closes the existing child fences. Clearing a conversation
invalidates and removes executable continuation content, not merely visible
messages. Content-free usage outbox obligations are owner-scoped outside the
conversation cleanup.

Previously supplied memory binds its preference and exact record versions.
Current memory/resource access is rechecked before forwarding saved context.
If revoked context cannot be excluded without replaying accepted work, the run
stops clearly and retains the historical receipt for work already performed.

## Storage and operation

Coordination uses the existing `workflows` owner partition, separate from asset
definitions via the shared reserved namespace and `recordKind` discriminator.
Private checkpoints use the existing `messages` session partition. Checkpoint
and public-message changes CAS the active deletion sentinel in one batch.
SDK-consumed per-operation options are copied on every attempt.

Four active runs, row limits, escaped byte budgets and reserved transition space
bound coordination. Missing or malformed required state fails closed; there is
no empty-state reset or automatic unknown-hold cleanup. A full state is
unavailable, not silently truncated permission.

Financial HTTP responses retain explicit currency, scope and unknown nulls even
when those values originated as model defaults. The API and browser share a
financial response fixture covering uncapped/capped budgets and new/legacy
approvals; omitting a discriminator is a contract failure, not a free amount.

Usage records have stable operation identities and strict outbox delivery.
Duplicate acknowledgment confirms the same row; it does not record another
charge or rerun the operation. Execution receipts retain their ordinary 32 KiB
escaped-serialization budget and never contain hidden reasoning.

Source implementation, approved activation, a live restart/approval/timer
observation and issue closure are separate milestones. No production activation
or successful live exercise is implied by this document.
