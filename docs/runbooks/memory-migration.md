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

Run from `app/api`. The project environment supplies `azure-identity` and
`azure-cosmos`; the retired PostgreSQL driver is intentionally absent from
`pyproject.toml`. Supply it ephemerally with `uv run --with asyncpg` on every
migration invocation so it never becomes a runtime dependency:

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
uv run --with asyncpg python ..\..\scripts\migrate-memory-to-cosmos.py `
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

> **CI deployment (this repo):** production ships through the `deploy.yml`
> GitHub Actions workflow, not a local `azd up`. The equivalent freeze is to set
> the `AI4IA_MEMORY_STORE` repository variable to `disabled` (the normal
> repository/parameter default is now `cosmos`; the old CI-only `disabled`
> fallback was removed after cutover) and run the workflow:
>
> ```powershell
> gh variable set AI4IA_MEMORY_STORE --body disabled
> gh workflow run deploy.yml -f provision=true
> ```
>
> Everywhere below that says `azd env set AI4IA_MEMORY_STORE <value>; azd up`, the
> CI equivalent is `gh variable set AI4IA_MEMORY_STORE --body <value>` followed by
> `gh workflow run deploy.yml -f provision=true` (or a push to `main`).

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
uv run --with asyncpg python ..\..\scripts\migrate-memory-to-cosmos.py `
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

**Executed 2026-08-06.** All five preconditions below were verified before the
IaC, configuration, application code and portal claims were removed. Recorded
here rather than in a commit message because this is the evidence someone will
want when they ask why a database disappeared.

1. **No rollback depends on PostgreSQL.** `AI4IA_MEMORY_STORE=cosmos` in
   production, the Cosmos `memories` container exists partitioned on `/userId`,
   and the workspace recorded 99 `memory_operation` events in the preceding 30
   days. Memory is demonstrably being served from Cosmos, not merely configured to.
2. **Azure AI Search is authoritative for document chunks.** `_build_chunk_store`
   in `app/api/src/ai4ia_api/library/ingest_factory.py` returns
   `AzureSearchDocChunkStore` whenever `search_endpoint` is set, and it *is* set
   in production — so the PostgreSQL branch was already unreachable. Nothing was
   accepted as a loss; the fallback had no live traffic to lose. That branch and
   its ~215-line `PgDocChunkStore` are now deleted rather than left as
   unreachable, untested code.
3. **Evidence archived.** Server `psql-ai4ia-slurmfactory-centralus-vypvgrncoed2o`,
   Central US, PostgreSQL 16, `Standard_B2s` Burstable, 32 GB, 7-day backup
   retention, `earliestRestoreDate` 2026-07-31. Connection metrics returned no
   datapoints over the window — the server was idle.
4. **Explicit destructive-action approval** given by the accountable owner.
5. **Removal.** IaC (`data.bicep`, `api.bicep`, `main.bicep`,
   `main.parameters.json`), the `AI4IA_POSTGRES_*` and
   `AI4IA_METRICS_POSTGRES_RESOURCE_ID` settings, the admin dashboard's Postgres
   panel, the `asyncpg` runtime dependency, and every documentation/portal claim
   that PostgreSQL is a deployed component. `scripts/tests/test_postgres_retired.py`
   fails if any of that returns.

### Deleting the server itself

Not done by this change, and **deliberately sequenced after it**, because
`azd provision` would otherwise recreate the server between the IaC merge and the
deletion.

> **Deleting a PostgreSQL Flexible Server destroys its backups with it.** Unlike
> Cosmos, there is no post-deletion point-in-time restore — the 7-day window ends
> the moment the server does. Cosmos DB keeps a restorable copy of a deleted
> account for its configured retention; Postgres does not. Treat this as
> irreversible.

Recommended order:

1. Merge the IaC removal (this change) so nothing recreates the server.
2. **Stop** the server — reversible, and it drops roughly 85% of the cost while
   the decision settles:
   ```powershell
   az postgres flexible-server stop -g rg-ai4ia-<env> --name psql-<...>
   ```
3. Delete only after the observation window closes with the server stopped and
   nothing having missed it.

> ⚠️ **A stopped Flexible Server restarts itself after 7 days.** Azure prints this
> as a warning on `stop` and it is easy to miss: *"Server will be automatically
> started after 7 days if you do not perform a manual start operation."* Stopping
> is therefore a **cost pause, not a decommission** — leave it and the server
> silently resumes billing, still holding data nothing reads. Either delete within
> the window or re-issue `stop` each week. Do not treat step 2 as the end state.

### Status (complete)

| Step | State |
| --- | --- |
| IaC / code / docs removal | **Done** — #293, guarded by `scripts/tests/test_postgres_retired.py` |
| Server stopped | **Done 2026-08-07.** `psql-ai4ia-slurmfactory-centralus-vypvgrncoed2o` reports `Stopped`. Verified idle first: `active_connections` returned **no datapoints at all** over the preceding 7 days |
| Server deleted | **Done 2026-08-07**, on explicit owner instruction. `az postgres flexible-server show` now returns `ResourceNotFound` and the resource group holds no PostgreSQL servers. The 7-day backup window went with it, as warned above — there is nothing left to restore from |

## Common failures

> The migration script keeps its `--postgres-*` flags and its tests, so a restored
> backup could still be migrated. Invoke it with `uv run --with asyncpg ...`;
> that driver was removed from runtime dependencies because no API source imports
> it any more.

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
