# Runbook: PostgreSQL memory to Cosmos

This runbook migrates owned rows from the legacy `mem0_memories` pgvector table
to the canonical Cosmos `memories` container. It is intentionally manual,
dry-run-first, and separate from deployment automation.

> **Approval boundary:** repository preparation does not authorize a live
> deployment, Cosmos vector-capability change, production data write, RBAC
> change, or PostgreSQL deletion. Confirm the target subscription/region and get
> explicit approval for each live action.

## Safety properties

The migration tool:

- reads PostgreSQL and never updates or deletes source rows;
- defaults to dry-run and requires `--apply` to write Cosmos;
- accepts only a plain SQL identifier for the source table;
- uses the API managed identity/Azure credential path instead of a SQL password;
- quarantines missing ownership, malformed payloads, and invalid vectors;
- writes deterministic target ids, so reruns verify instead of duplicating;
- writes plaintext/vectors only to the target memory item;
- emits quarantine source-id hashes and reason codes, never memory text;
- refuses any partition already observed by the Cosmos runtime (the runtime
  creates/touches its state item before recall or mutation), preventing a rerun
  from recreating a memory a user explicitly deleted after cutover;
- can verify ids, text hashes, dimensions, per-user counts, and sampled vector
  recall.

It does not import replica-local SQLite history.

## Required posture

Before a live run:

1. Record the source revision, PostgreSQL server/database/table, target Cosmos
   account/database/container, expected row count, and change owner.
2. Confirm the Cosmos account/container configuration:
   - `EnableNoSQLVectorSearch`;
   - container `memories`;
   - partition key `/userId`;
   - `/embedding`, 3,072 dimensions, cosine, `quantizedFlat`.
3. Confirm the operator identity can:
   - acquire `https://ossrdbms-aad.database.windows.net/.default`;
   - connect to PostgreSQL as the configured Entra role;
   - read/write the target Cosmos data plane.
4. Confirm the source embedding dimension/model matches the target arguments.
5. Keep PostgreSQL, diagnostics, and its data intact through the rollback window.
6. Schedule a memory write freeze. Chat may continue while memory is disabled.

## Configuration

Run from `app/api` so the locked API environment supplies `asyncpg`,
`azure-identity`, and `azure-cosmos`:

```powershell
Set-Location app\api
uv sync --extra dev

$env:AI4IA_POSTGRES_HOST = "<server>.postgres.database.azure.com"
$env:AI4IA_POSTGRES_DATABASE = "mem0"
$env:AI4IA_POSTGRES_USER = "<api-managed-identity-role>"
$env:AI4IA_COSMOS_ENDPOINT = "https://<account>.documents.azure.com:443/"
$env:AI4IA_COSMOS_DATABASE = "ai4ia"
```

Do not place these values in source files. The host/endpoints are not secrets, but
the selected Azure identity and environment still determine production access.

## Phase 1: pre-freeze dry run

Dry-run needs PostgreSQL access but does not need a Cosmos endpoint:

```powershell
uv run python ..\..\scripts\migrate-memory-to-cosmos.py `
  --source-table mem0_memories `
  --dimensions 3072 `
  --embedding-model text-embedding-3-large `
  --quarantine-file "$env:TEMP\ai4ia-memory-quarantine.jsonl"
```

Review the single JSON summary:

- `scanned = valid + quarantined`;
- `created` and `unchanged` are absent in dry-run mode;
- every quarantine reason is understood;
- no user ownership is inferred or repaired automatically.

The quarantine file contains only `sourceIdHash` and `reason`. Resolve bad source
data through a separately reviewed process; never guess a `user_id`.

Because legacy memory can still change, this result is planning evidence only.
Repeat it after the freeze.

## Phase 2: deploy the freeze

Set the deployment backend to disabled and deploy the prepared revision:

```powershell
azd env set AI4IA_MEMORY_STORE disabled
azd up
```

This deploy also prepares the Cosmos vector capability/container while the new
API revision performs no recall or writes. Verify:

1. the new API/web revision SHAs match the approved commit;
2. `GET /api/memories` reports `status="disabled"`;
3. old API revisions are drained/deactivated;
4. chat still works without memory;
5. no process is writing the legacy memory table.

Do not proceed if an old revision can still write memory.

## Phase 3: final dry run

Repeat Phase 1 after the freeze and preserve the timestamped summary. The final
quarantine decision and valid count become the apply baseline.

## Phase 4: apply and verify

With explicit production-data-write approval:

```powershell
uv run python ..\..\scripts\migrate-memory-to-cosmos.py `
  --source-table mem0_memories `
  --dimensions 3072 `
  --embedding-model text-embedding-3-large `
  --quarantine-file "$env:TEMP\ai4ia-memory-quarantine-final.jsonl" `
  --apply `
  --verify `
  --recall-samples 5
```

Expected results:

- `created + unchanged = valid`;
- `verified = valid`;
- per-user migrated counts match the valid source rows;
- sampled vector queries return their deterministic target ids;
- a rerun reports the same records as `unchanged`, not duplicates.

Stop on any exception or count mismatch. Do not enable Cosmos memory after a
partial/unverified run. Deterministic ids make the same command safe to retry once
the cause is corrected, provided target partitions have not accepted live
mutations.

## Phase 5: activate Cosmos

After apply/verify succeeds:

```powershell
azd env set AI4IA_MEMORY_STORE cosmos
azd up
```

Verify with two non-admin test users:

1. each sees only their own migrated records;
2. recall returns an expected migrated fact;
3. create returns an ETag and a repeated idempotency key does not duplicate;
4. edit with the current ETag succeeds and a stale ETag returns `409`;
5. delete removes text and vector from active reads/search;
6. `/forget` removes session scope and `/forget me` removes user scope;
7. a post-forget create survives;
8. document save, resave, forget, and source deletion behave atomically;
9. logs and migration artifacts contain no memory text/vector.

Record revision ids, timestamps, valid/quarantined counts, verification output,
and smoke-test evidence.

## Rollback

There is no automatic reverse migration and no dual-write.

If Cosmos memory must be removed from service:

```powershell
azd env set AI4IA_MEMORY_STORE disabled
azd up
```

This preserves chat while stopping memory reads/writes. A prior application
revision may be reactivated only with memory still disabled. Do not silently
re-enable the legacy mem0 backend: post-cutover Cosmos changes would be absent
from PostgreSQL, its deletion semantics are weaker, and the new runtime no longer
ships that backend.

Leave migrated Cosmos records and PostgreSQL source data intact while diagnosing.
A return to another writable backend requires a separately designed, approved,
and verified migration.

## Retirement

After the agreed observation window:

1. confirm no rollback depends on PostgreSQL;
2. confirm Azure AI Search is authoritative for document chunks, or deliberately
   accept losing the PostgreSQL document-index fallback;
3. archive the migration evidence and production revision ids;
4. obtain explicit destructive-action approval;
5. remove PostgreSQL IaC, configuration, diagnostics, firewall/admin assignments,
   and data in a separate reviewed change.

Deleting the server is not part of this migration implementation.

## Common failures

| Failure | Meaning / response |
| --- | --- |
| Missing PostgreSQL host/user | Supply the approved Entra connection settings |
| Invalid source-table name | Use a plain identifier; do not pass schema/SQL fragments |
| Quarantined ownership | Stop; ownership must not be guessed |
| Wrong embedding dimensions | Confirm the source model/vector schema before retrying |
| Target partition has mutations | The freeze was violated or cutover already began; do not resurrect legacy rows |
| Existing target does not match | Treat as corruption/collision; inspect metadata without logging content |
| Migrated count mismatch | Keep memory disabled and reconcile source/target partitions |
| Sampled recall misses | Confirm vector policy, dimensions, index readiness, and target data |
