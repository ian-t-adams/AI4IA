# Resumable conversation deletion

The opt-in protocol removes a conversation from normal use before cleanup and
retains enough owner-scoped state to resume cleanup after interruption. It does
not promise instantaneous physical erasure, provider-backup deletion, or an erase
deadline. Source implementation, approved activation, and existing-record
migration are separate acceptance states.

## Scope and retained evidence

| Resource | Treatment |
|---|---|
| Conversation title, instructions, summary, consent and associations | Replaced by minimal deletion metadata before child cleanup |
| Messages, receipts embedded in those messages, session attachment metadata/text | Removed from the existing session partitions by bounded cleanup |
| Retained inline original bytes | Only the original authenticated owner/session prefix in the originally recorded inline Blob target |
| Owner tombstone, closed child fences | Retained indefinitely, with no application TTL or id reuse |
| Unresolved upload-attempt records | Retained until the original PUT conclusively returns and its completion is acknowledged; no automatic expiry |
| Reusable library documents, Search indexes, memories | Not deleted by conversation cleanup |
| User-scoped generated images/video and processed exports | Not deleted; these are not conversation-owned resources |
| Provider sandbox files, backups, Blob versions/soft-deleted copies, service telemetry | Outside this verification scope and subject to their separate retention policies |

Deleting a conversation does not retroactively cancel an already issued model,
tool or storage request. The protocol prevents subsequent ordinary access and
fences child persistence; in-flight external uploads are accounted for separately.

## Default-off and new conversations only

`AI4IA_SESSION_DELETION_ENABLED` defaults to `false` in code, Bicep and the azd
profile. `AI4IA_SESSION_DELETION_ROLLOUT_ID` defaults to empty. The deployment
workflow forwards both as non-secret variables but does not create approval
records, migrate data or start a background reconciler.

With the flag disabled, unversioned conversations retain the existing 204
best-effort delete behavior. That legacy cascade is **not** verified against
concurrent late writers. When the flag is enabled, unversioned records return
409 `migration_required`, without starting cleanup. They are never silently
enrolled or routed to legacy deletion under the new contract.

Only newly created conversations get protocol version 1, a unique generation,
and typed fences. Existing canonical partition keys remain unchanged:
`sessions` uses `/userId`; `messages` and `documents` each use `/sessionId`.

Protocol guards continue to apply to v1 records even if the feature is later
disabled. A v1 tombstone or fence can never be removed by the legacy delete path.

## Activation requires reviewed evidence

Outside local, configuration requires Cosmos and Entra plus a syntactically valid
rollout record id. Offline preflight checks these settings, but cannot prove a
worker cutover occurred. Enabled API startup reads only the specifically selected
approval record and required storage metadata. It refuses:

- Missing/mismatched approval, version, scope or cutover/recovery references.
- More than one writable region, multiple-write-location support, or a
  consistency level other than the current IaC's Session setting. Other
  consistency modes need their own reviewed token/observation contract.
- Incorrect partition paths, an expiring container default TTL, or enabled
  analytical storage on the three conversation containers.

The approval record lives in the existing `sessions` container with
`userId="__ai4ia_deletion_control__"` and its `id` equal to the configured rollout
id. Its schema is:

```json
{
  "id": "<approved-rollout-id>",
  "userId": "__ai4ia_deletion_control__",
  "kind": "session_deletion_rollout_v1",
  "protocol": 1,
  "state": "approved",
  "scope": "new_sessions_only",
  "singleWriteRegion": true,
  "noCoordinationExpiry": true,
  "writerCutoverEvidence": "<reference to reviewed old-worker drain evidence>",
  "recoveryReviewEvidence": "<reference to approved recovery and retention review>"
}
```

This is a schema illustration, **not** an approval to create it. There is no API,
startup repair, Bicep data write or migration script that authors this record.
The application checks references and layout; it does not independently establish
the truth of the referenced operator evidence.

Before authoring live approval, Ian Adams must approve evidence that every writer,
including API replicas, streaming callbacks, voice persistence, workflow workers
and delayed uploads, is running the compatible protocol or is conclusively
drained. Old workers ignore the new sentinels. A flag, elapsed waiting period or
passing local test cannot replace that evidence.

An existing-record cutover additionally requires a separate dry-run inventory,
reviewed recovery/retention policy, and approved enrollment procedure. None is
performed by this source change. Do not close #435 on source tests alone.

## Why the fences close the Cosmos race

Initialization conditionally creates a minimal, non-readable owner reservation
first. It contains only owner/session/generation, creation time, retention and
artifact-target metadata: no title, instructions, summary, tools, associations
or user payload. It then conditionally creates an active sentinel in each child
partition and publishes the full conversation from the live request using the
reservation's **original ETag**. Both matching sentinel writes must be
acknowledged before publication can return success.

A racing deletion first replaces the reservation/parent with an owner tombstone
and can create a missing sentinel only in the **closed** state. Initialization
never upserts, adopts or reopens an existing sentinel and cannot overwrite the
tombstone. If publication wins first, the explicit owner deletion re-reads and
tombstones that active conversation; if discard wins, the initializer cannot
return 201. No new child or upload reservation is admitted before an active
parent exists.

Every v1 message/document mutation includes an ETag-conditional replacement of the
active sentinel in the **same** Cosmos transactional batch. This includes message
create/upsert, summary replies, workflow paired claims, expected-message/status/
lease checkpoint replacement, approval consumption, voice-derived messages, and
document create/delete/clear paths. A mutation either commits before closure and
is subsequently eligible for cleanup, or loses the batch precondition and writes
nothing. This is a per-container proof, not a cross-container pseudo-transaction.

Ordinary v1 parent reads use a bounded server-evaluated CAS barrier. A stale
Session-consistency snapshot from another API replica cannot grant access after
deletion; repeated stale snapshots become unavailable. This costs additional
Cosmos requests/RUs and is deliberately opt-in.
The retained-original read path also rechecks the canonical attachment and parent,
so cached analysis-tool closures cannot read bytes merely because Blob cleanup
has not yet reached them.

Cleanup re-establishes closed-fence barriers and passes each operation's own
partition session token to child queries. A one-time empty eventually consistent
scan is not accepted as absence evidence. The request-specific SDK response
headers are used, not the client's shared last-response headers.

Single-partition transactional co-location of parent and children could provide a
stronger shared atomic unit, but would require approved repartitioning/migration
and would still not transact with Blob or provider backups. It is not implemented.

## Interrupted creation has explicit owner recovery

Crashes after the reservation, after either sentinel, or just before publication
leave a retained, content-free `session_initializing_v1` record. It is not parsed
as an ordinary conversation. `GET /api/sessions/initializations` lists only the
authenticated owner's incomplete ids and creation times, with bounded keyset
pages and opaque cursors. Reads never acquire a lease, create a missing sentinel,
advance initialization, or clean data.

This is a read-only observation under Cosmos Session consistency, not a completion
certificate. The response says `observation="not_completion_evidence"`. An empty
page does not establish that a previously failed creation succeeded; refresh or
investigate a known failure. Malformed records/cursors surface errors rather than
being converted to an empty success.

The owner can explicitly **Discard incomplete creation** using the existing
DELETE endpoint, followed by **Resume cleanup**. Discard also prevents an
in-flight creator from later publishing: its original reservation ETag is stale.
The missing-fence close rule covers reservations with zero, one or two sentinels.
The original user payload was deliberately not persisted, so there is no
automatic initialization resume or invented empty conversation. Starting again is
a new user create request with a fresh server id.

If publication succeeded but its HTTP acknowledgment was lost, the active
conversation is discoverable through the normal conversation list, not presented
as incomplete initialization. Existing reservations, active parents, tombstones
and closed sentinels all reject id reuse. No age threshold, TTL, migration or
global orphan cleanup is used for this recovery.

## Blob uploads are not Cosmos transactions

Before a v1 inline PUT, the repository records the exact storage target identity
and conditionally creates an upload-attempt ticket behind the document fence.
The target is stored as a hash of the account/container configuration; changing
configuration cannot make an unrelated empty container pass cleanup.

Ticketed v1 retained-original PUT retries are disabled; legacy/flag-off uploads
keep the existing retry behavior. A successful retry could otherwise
return while an earlier timed-out attempt remains in flight. The application
acknowledges the ticket only after the PUT conclusively returns. If an upload
times out, is cancelled, crashes, or succeeds but loses its Cosmos acknowledgment,
the ticket remains unresolved. Failed optional retention may still allow a
text-only document, but its unknown upload is not forgotten.

Deletion closes the document fence before enumerating tickets. No new tickets can
then appear; existing uncertain PUTs can still complete. Bounded strict Blob cleanup
may remove what exists now, but `attachmentsVerified` remains false while any
unresolved ticket exists. A later owner-requested pass removes bytes that arrived
after an earlier empty scan. Listing/delete failures surface as retryable, never
as successful zero deletions.

Status includes a bounded sample of unresolved ticket/document ids and start
times, with a truncation indicator. These are investigation references, not an
invitation to force-complete a ticket. For an irrecoverable ticket, preserve its
evidence and escalate to the operator with those ids. Establish the original
request's terminal state from reliable worker/provider evidence if available.
An empty container, missing manifest, expired coordinator lease, or lack of logs
is not that proof.

There is no user force-complete button or unaudited administrative bypass. If
terminal evidence is unavailable, cleanup remains pending; an audited recovery
mechanism or stronger storage fencing needs a separate design and live approval.

## Owner API and recovery workflow

| Operation | Contract |
|---|---|
| `DELETE /api/sessions/{id}` | Enabled v1: persist intent and return 202/pending; repeat requests return retained status. Already verified can return 200. Disabled legacy: 204 best effort. |
| `GET /api/sessions/deletions` | Owner-only status pages; `hasMore` and `nextCursor` identify additional pages. No cleanup side effects. |
| `GET /api/sessions/initializations` | Read-only owner recovery observations for interrupted creation, with opaque pagination. Not evidence of completion. |
| `GET /api/sessions/{id}/deletion` | Owner-only last recorded status, including unresolved uploads. Read-only. |
| `POST /api/sessions/{id}/deletion/reconcile` | Explicitly resume one bounded pass; 202 for pending/retryable work, 200 for scoped verified cleanup. |

The sidebar's deletion status surface can be reopened after a reload. Refresh
reads status; **Resume cleanup** explicitly requests work. Nothing polls a
destructive endpoint, and there is no autonomous recovery worker in this slice.
After a crash, the durable record is still discoverable; the owner resumes it.

Each pass processes at most 25 records per child surface and 25 Blob objects.
CAS loops have three attempts; a pass has a 20-second execution budget, plus at
most five seconds to persist retryable failure evidence. A
60-second coordinator lease only controls checkpoint ownership, not upload
termination. ETag-bound progress prevents a stale worker from overwriting a
new lease holder. Reads of deletion status do not acquire leases or mutate
progress. Concurrent callers see existing pending status or explicit unavailable
errors rather than obtaining two valid checkpoint owners.

`pending` means more work or unresolved external requests remain. `retryable`
records a bounded reason such as storage unavailable, timeout, changed
coordination, integrity mismatch or a missing original artifact target.
`cleanup_verified` records `lastVerifiedAt` for conversation content and inline
originals only; coordination metadata stays retained and `backupsErased` remains
false. No result establishes an erase deadline or changes provider retention.

## Rollback and remaining acceptance

Before any v1 records exist, leaving the flag off preserves the legacy path.
After v1 creation, disabling the flag pauses enrollment/new cleanup while compatible
binaries continue to enforce guards and serve owner status. **Do not roll back to
older binaries that ignore protocol markers.** Restore/failover procedures must
preserve the tombstones, generation and closed fences together; restoring active
data without its deletion evidence can resurrect a conversation.

Local and transactional-fake tests cover interrupted cleanup, stale writers,
delayed uploads, ownership, checkpoint contention and bounded progress. They do
not establish live cutover, old-record migration, physical-retention acceptance,
service-backup purge or unattended operations. Those remain approval-gated work
under #435.
