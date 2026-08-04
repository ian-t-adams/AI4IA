# Changelog

All notable changes to AI4IA will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses date-based release notes until formal versioning is introduced.

## [Unreleased]

### Added

- **Workflow runner UI: run, see the result, and know what a step can do**
  (`WorkflowBuilder.tsx`, `WorkflowRunReport.tsx`, `workflowCapabilities.ts`,
  `workflowRun.ts`). Running a workflow used to appear to produce nothing: the builder
  called `onRun(sessionId)` the instant a run returned, which unmounted the panel and
  took the output with it. The result now renders **in place** — a per-step trace, the
  elapsed time, and explicit `Open in chat` / `Run again` buttons — so hand-off is a
  choice rather than a side effect. Alongside it:
  - **Per-step capability chips.** A workflow step's tool surface is *not* its agent's
    tool list, and nothing said so. Document reading and web search are ambient (every
    step gets them); the two memory tools are the only ones that must be deliberately
    attached; and `generate_image` / `process_document` / MCP tools work in chat and are
    structurally absent from a step. That asymmetry is why a workflow asked to
    "remember the decisions" ran, replied that it could not save anything, and was
    recorded as a **success**. Each step now states which of these it has, with a route
    back to the **Build** tab — where the per-step tool checkboxes live — when the
    remedy is "switch it on". (The Build tab itself renders no button: its checkboxes
    sit directly below the chips, so one would be noise.) Where the server genuinely
    does not report a capability (Web IQ is injected unconditionally and never listed by
    `/api/tools`) the chip says "if configured" rather than inventing a verdict.
  - **Per-step failure attribution.** `runner.py` prefixes every fatal step error with
    `Step {n}: `, so the trace can say "step 2 failed, step 1 succeeded, step 3 never
    started" instead of "the workflow failed". No backend change. Where the prefix is
    absent the failure came from outside a step, and the trace deliberately claims
    nothing per-step rather than blaming an innocent row.
  - **Document scoping.** A run can be restricted to selected library documents. The key
    is omitted entirely when nothing is selected, because `[]` is not "no preference" —
    the API reads `allowed_document_ids is None or bool(...)`, so an empty array switches
    document reading *off* for the whole run.
  - **Build / Run & test tabs** replacing a single scrolling column. The durable
    "Keep running if the app restarts" checkbox previously sat in a 200px nav column and
    wrapped its label across five lines; a run form does not belong in a nav list, so the
    fix is structural rather than a wider column.

- **`remember_memory` tool** (`memory/remember_capability.py`). Agents can now *write* a
  short durable fact to the caller's own memory, not just recall one. Until now memory was
  only ever written by the passive path in the chat router (which stores the user's own
  utterance after a turn), so an agent asked to "read these notes and remember the
  decisions" correctly answered that it could not — and a workflow built for that job could
  never have worked. The tool is user-attachable, closure-bound to the authenticated user
  (the model cannot name a different one), capped per turn and per fact, and reports its
  outcome **honestly**: `MemoryService.remember` now returns whether anything was durably
  written, so a skipped or failed write is never reported as "saved". A fact already covered
  by an existing memory is reported as "nothing new stored" — a distinct outcome from a
  failure, so the model does not retry a write that was correctly declined.
- **Shared agent capabilities** (`agents/capabilities.py`). The execution-mode-independent
  synthetic tools — `fetch_document`, the five Web IQ tools, `recall_memory`,
  `remember_memory` — are now assembled in one place used by plain chat, agent turns, and
  workflow steps alike, so the three surfaces cannot drift into different tool sets.
  Deliberately excluded and still chat-only: `generate_image` / `generate_video` /
  `process_document` (they deliver results as message attachments through a per-turn sink
  that only the chat router drains), compute/inline-analysis (gated on a per-turn
  classification), MCP (it replaces the registry rather than adding to it), and
  `delegate_to_agent` (workflows reject orchestrator steps by construction).

- **Durable workflow execution** (`workflows/durable.py`, `infra/modules/durabletask.bicep`).
  `POST /api/workflows/{name}/run` accepts an opt-in `"durable": true` that schedules the
  run on an Azure Durable Task Scheduler orchestration and returns `202` with a run id,
  polled via `GET /api/workflows/runs/{run_id}`. Previously every workflow executed
  synchronously inside the HTTP request and died with the replica on deploy, scale-in, or
  crash. Both paths share one implementation (`run_workflow_step()`), so entitlement,
  tool-authorization, and step-budget guards cannot drift between them, and durable steps
  still route model calls proxy → APIM → Foundry.
  **Enabled in production 2026-08-02**; the `${AI4IA_ENABLE_DURABLE_WORKFLOWS=true}`
  token is retained so another environment can still opt out. (The first attempt at
  enabling it did not take effect — see the shadowed-default fix under Fixed — so the
  scheduler was actually provisioned by the follow-up deploy.) With the flag off,
  `"durable": true` returns `422` rather than
  silently falling back to synchronous execution. Data-plane RBAC is granted at **task-hub**
  scope so a future second app on the same scheduler cannot read this app's payloads, and
  run ids are `<userId>:<uuid4>` so ownership is checked *before* any fetch.
- **Resource-provider preflight in the deploy workflow** (`deploy.yml` →
  `scripts/check-resource-providers.py --register`). An unregistered provider fails
  `azd provision` roughly ten minutes in with `MissingSubscriptionRegistration`, after real
  resources exist. The script already derived its required set from `infra/**/*.bicep`, but
  nothing ran it outside a manual runbook step — so it protected a greenfield standup and
  not the routine path where a new provider arrives with a feature. Enabling durable
  workflows is exactly that case: `Microsoft.DurableTask` was `NotRegistered` in the live
  subscription, because a flag-gated module never submits its resource type while the flag
  is off. Pinned by tests asserting the step runs *before* `azd provision` — a step moved
  after it still reads as present while doing nothing.
- `codeInterpreterRawFilesEnabled` Bicep parameter, wiring the previously unreachable
  `AI4IA_CODE_INTERPRETER_RAW_FILES_ENABLED` setting through `main.bicep` → `api.bicep`.
  The feature was fully implemented (`library/compute_capability.py` uploads a document's
  *original* bytes to the code-interpreter container instead of Content-Understanding
  parsed text) but defaulted to `false` with zero Bicep wiring, so it could not be turned
  on in production without a code change. Now enabled.
- Guard test pinning the `owner` placeholder warning (`scripts/tests/test_feature_prereqs.py`),
  negative-tested so it cannot go vacuous.
- Root contributor guide, governance files, community templates, and third-party notices.
- Orange/blue/near-black brand identity across the app and the GitHub Pages portal,
  replacing the indigo + blue-teal palette. Regenerated marks, Open Graph lettermark, and
  favicon from a new reproducible generator (`scripts/gen-brand-assets.py`); the four
  image assets went from 1.65 MB to 79 KB, and the lettermark moved from a square to the
  1200x630 Open Graph ratio social consumers actually expect.
- Contrast gates for both palettes, since an illegible colour renders fine and fails
  silently: `app/web/src/app/globals.contrast.test.ts` (rewritten, 27 assertions) and
  `scripts/tests/test_portal_contrast.py` (new, run by `quality` because the static
  portal has no build step of its own). The portal gate catches four pre-existing AA
  failures in the palette it replaced.
- Live Planet Express deployment documentation for tenant
  `6907d2a4-685a-4aea-92ab-d930217467f1`, subscription
  `sub-planetexpress-slurmfactory`, resource group `rg-ai4ia-slurmfactory`, and
  the `ai4ia.nomad-analytics.com` / `genaiproxy.nomad-analytics.com` custom domains.
- `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` to the model catalog
  (GlobalStandard in East US 2 + Sweden Central, version `2026-07-09`, 1,050,000-token
  context). All three are GA, need no access request, and already carry default quota in
  the target subscription. They serve both Chat Completions and the Responses API, so they
  route through the existing chat path unchanged.
- Subscription preflights for standing the stack up in a new tenant/subscription:
  `scripts/check-resource-providers.py` (derives the required resource providers from
  `infra/**/*.bicep`; `--register` registers and waits) and
  `scripts/check-model-availability.py` (checks `infra/models.json` against what the
  target subscription is actually entitled to deploy, per region). Both are documented
  as step 0 of the deployment runbook's new-tenant procedure.
- `app/api/tests/test_lazy_imports_are_declared.py`, which re-derives every third-party
  module imported inside a function body from the source AST and fails if one no longer
  resolves. The API imports heavy/optional SDKs lazily on purpose, which meant no gate
  noticed a missing dependency: with `azure-monitor-query` uninstalled outright, `pyright`
  reported `0 errors` (an unresolved submodule of the `azure` *namespace* package does not
  fail the type gate the way a missing top-level module does) and `pytest` stayed green
  (the import is lazy and the metrics tests inject a fake querier). 24 modules are covered,
  including `azure.cosmos.aio`, `azure.identity.aio`, `azure.storage.blob` and `pypdf`.
- `scripts/tests/test_dependabot_config.py`, which fails if `.github/dependabot.yml` and
  the `uv lock --check` CI gate are ever paired incompatibly again.
- A `uv lock --check` gate in `app-ci`, so `app/api/uv.lock` can no longer rot unnoticed
  behind `pyproject.toml`.

- **Per-step tool grants for workflows** (`workflows/models.py`, `workflows/runner.py`,
  `WorkflowBuilder.tsx`). A step can now be granted tools its agent does not carry, via
  a per-step `extraTools` list rendered as **Tools for this step**. This closes a gap
  that was unreachable by construction rather than merely awkward: `remember_memory` is
  attach-gated, the curated agents ship **fixed** tool lists in `data/agents.json`
  (`researcher` and `writer` carry none at all) and are not editable in the agent
  builder, and `/api/agents` does not expose `tools`. So for the agents most users pick,
  the capability could not be switched on from anywhere in the product — and the model,
  never told it lacked the tool, narrated a memory save it had not performed while the
  run was recorded as a success.
  - Grants are **additive**: `[]` is exactly today's behaviour, so nothing migrates, and
    a step can never silently drop the agent's own `mcp:` tools (those are per-user and
    dynamic, so `WorkflowService` cannot re-validate them).
  - The allowlist is **server-authoritative**. The web offers a derived
    `STEP_ATTACHABLE_TOOLS` (attachable minus chat-only minus MCP) rather than a
    hand-written list, which would have offered `generate_image` — a control that saves
    and validates cleanly and then does nothing, i.e. a brand-new inert control of
    precisely the class being fixed.
  - Synthetic tools (`remember_memory`) and registry tools (`calculator`) reach the model
    by **two independent paths** (`extra_tools` vs `tool_names`); each has its own test,
    because reverting either leaves the other's test green.

### Removed

- Four dead symbols, each confirmed by a repo-wide search returning only its own
  definition plus test call sites: `revokeDocumentShare` (`app/web/src/lib/api.ts` — the
  UI removes a grantee by re-PUTting the full list via `setDocumentShares`, so the
  per-grantee DELETE helper never had a caller), and `clear_quarantine`, `health_status`,
  and the `McpHealthStatus` enum (`agents/mcp_health.py`) plus `telemetry_enabled`
  (`logging_setup.py`). Two of these carried docstrings that actively *claimed* a
  production caller — `clear_quarantine` said it was "used by the explicit user-initiated
  reconnect path" when that path calls `record_success` instead. The coarse health-status
  precedence now has a single owner, `app/web/src/lib/customTools.ts`, and its comment no
  longer cites a deleted Python symbol. The server endpoint
  `DELETE /documents/{id}/shares/{email}` was deliberately **kept**: it is public,
  server-tested API surface, and removing it is a breaking change needing its own review.
- Three unreferenced symbols and an unused Bicep parameter, each confirmed dead by a
  repo-wide search returning only its own definition: `ErrorResponse` in `errors.py`
  (never imported or constructed — every error body is a dict from `build_error_payload`),
  `VoiceProviderSelectionMode` (the `selectionMode` fields are `Literal` types, not this
  enum), the TypeScript `AzureOpenAIVoiceProvider` type in `voiceLive.ts` (distinct from
  the Python class of the same name, which `realtime.py` does use and which is untouched),
  and `environmentName` in `web.bicep`, which the Bicep linter had been flagging as
  `no-unused-params`.

- The legacy Consumption-tier APIM (`apim-<workload>-<env>-<suffix>`) and every child
  resource — its model/realtime APIs, policies, policy fragments, subscriptions,
  diagnostics, Foundry role assignments, and the unused
  `legacyConsumptionApimGatewayUrl` output — plus
  `infra/policies/realtime-routing-legacy.xml`. It existed only as an HTTP/SSE rollback
  plane during the Basic v2 migration and had been inactive since. `gateway.bicep` now
  creates no APIM service of its own and attaches its children to the existing
  `apim-mcp-*` Basic v2 service, which is the only APIM in the environment. Removal was
  gated on evidence: no live resource referenced the Consumption gateway URL, the module
  output was consumed by nothing, and an end-to-end chat through
  SimpleL7Proxy -> APIM -> Foundry returned `200` with annotate-only RAI intact both
  before and after the deletion. Realtime never had a Consumption rollback path
  (Consumption does not support WebSockets), so no capability was lost.

### Changed

- **APIM child entity names are derived from `${workload}` instead of hardcoded**
  (`apimcore.bicep`, `gateway.bicep`, `mcpgateway.bicep`). APIM children share one flat
  namespace per service, so the six hardcoded `ai4ia-*` product and subscription names meant
  a second workload onto the shared gateway would not fail loudly — it would *adopt and
  overwrite* this app's credentials. `workload` also gained an `${AI4IA_WORKLOAD=ai4ia}`
  override (previously the only parameter without one) and `deploy.yml` now derives
  `rg-${AI4IA_WORKLOAD:-ai4ia}-${AZURE_ENV_NAME}` rather than hardcoding the workload, so a
  renamed workload's post-deploy custom-domain steps look in the resource group that exists
  instead of silently no-opping against one that does not.
  **No live credential moved**: the default resolves byte-identically to the six deployed
  names, verified against the running APIM before merge, so ARM performs no replacement and
  no subscription key rotates (a rename would have re-keyed the proxy's model hop).
  `ApimChildNamingTests` in `scripts/tests/test_bicep_naming.py` fails any child name that
  reverts to a literal, and pins the `workload` default so a future edit cannot silently
  re-key the live gateway. The shared `openai`/`realtime` **APIs** are deliberately excluded:
  a second app reuses those rather than duplicating them.

- Cosmos DB moved from `Periodic` backup to **`Continuous7Days`**, taking the recovery window
  for the canonical store (sessions, messages, usage, memory, user agents, workflows, MCP
  records, document manifests) from roughly **8 hours behind a support ticket** to **any second
  in the last 7 days, self-service** via `az cosmosdb restore`. `Continuous7Days` is
  deliberate: it is the only continuous tier Azure does not charge backup storage for, so this
  is a ~21x larger recovery window at no recurring cost. Two properties of this knob make it
  unusually easy to get wrong, so both are now pinned by
  `scripts/tests/test_cosmos_backup_policy.py` (run by `infra-validate`): enabling continuous
  mode is **irreversible**, and the tier is a **silent billing boundary** — `Continuous30Days`
  and `Continuous35Days` are valid ARM that deploy and render identically while charging for
  backup storage. Note that the mode change cannot be made by editing Bicep and redeploying:
  Azure rejects a backup-mode change bundled with any other property update ("Cannot update
  continuous backup mode and other properties at the same time"), so the live account was
  migrated with a standalone `az cosmosdb update` and the Bicep restates the result to keep
  redeploys a no-op.

- The Voice Live Origin allowlist is now **derived in Bicep** from the web app each
  deployment creates, instead of being a hardcoded hostname in
  `infra/main.parameters.json`. `AI4IA_REALTIME_ALLOWED_ORIGINS` only adds extra origins.
- Operator scripts (`teardown.ps1`, `purge-soft-deleted.ps1`, `status-snapshot.ps1`,
  `inventory.ps1`, `seed-models.ps1`) no longer carry tenant-specific defaults; each
  requires its target explicitly or resolves it from the selected azd environment.
- The Conversation Inspector is now two levels instead of seven peer tabs. Seven tabs
  never fit the 360px rail — at `flex: 1 1 86px` with a 4px gap only three fit per
  344px row, so they wrapped 3/3/1 and the orphaned seventh stretched to full width.
  They are now three groups (**Setup**: model, instructions, agent & tools, Voice;
  **Context**: documents, memory; **Usage**) rendered as a fixed three-column grid,
  with each former tab becoming a WAI-ARIA accordion section inside its group,
  exclusive within the group so one section's content is on screen at a time exactly
  as before. Row count is now structural rather than a consequence of flex-basis vs.
  intrinsic label width, so the nav cannot go ragged at any width or font scale; the
  `@media (max-width: 720px)` two-column override was removed for the same reason
  (three items in two columns is ragged again). Tab labels also lost
  `white-space: nowrap` — with `--font-scale` at 2x a non-wrapping label overflowed
  its track instead of growing taller — and gained 44px touch targets, which still
  occupy less vertical space than three wrapped rows did.
- Curated and user-agent instruction sources are now visible read-only in the
  Conversation Inspector so inherited prompts can be audited without making those layers
  editable in the session panel.
- Dependabot manages `/app/api` through `package-ecosystem: uv` rather than `pip`. The pip
  ecosystem edits `pyproject.toml` and cannot see `uv.lock`, so every Python dependency PR
  it opened failed the lock gate until someone hand-ran `uv lock`. Confirmed working: the
  first batch under `uv` updated both files.
- `azure-monitor-query` raised from `>=1.4,<2` to `>=2.0.0,<3`. The `<2` ceiling existed
  only to retain `MetricsQueryClient`, which now comes from `azure-monitor-querymetrics`.
  Every attribute `metrics/log_analytics.py` uses was verified against the real 2.0.0 wheel
  before the bump.

### Fixed

- **Durable workflow runs silently dropped per-step fields** (`workflows/durable.py`).
  Both sides of the orchestration payload hand-listed the fields they carried —
  `build_orchestration_payload` emitted `{agent, instruction}` and `_step_from_dict`
  rebuilt from the same two — so a step's `extraTools` vanished in transit. A durable
  run therefore executed a **different step** than the byte-identical synchronous run,
  and did so silently: a step with no tools still returns 200. Both sides now use
  `model_dump(mode="json")` / `model_validate`, so payload and rebuild are exact
  inverses and any field added to `WorkflowStep` survives by construction. The
  regression test derives its expectations from `WorkflowStep.model_fields` rather than
  a hand-written list, because a hand-written list is what caused the bug; a second test
  pins that orchestration history written before a field existed still replays.
  Measured end to end: the same workflow that created **2** memories synchronously
  created **0** durably beforehand (the model saying plainly that it had no such tool)
  and **1** afterwards.

- **`remember_memory` had no checkbox.** The tool shipped correct in the API, in the
  allowlist, and in tests — and was unreachable from the product, because
  `app/web/src/lib/studio.ts` holds a hand-maintained mirror of the backend allowlist and
  the new tool was never added to it. The drift is silent in exactly one direction: a web
  list naming a tool the API rejects fails loudly with a 422 on save, while an API tool
  the web omits produces no error and no control. The existing guard could not see it
  (`toolHelp.test.ts` checks the web list against web help text — both sides live in the
  web, and a web missing a tool entirely is self-consistent), so
  `app/api/tests/test_attachable_tools_mirror.py` now compares the two across the
  language boundary.

- **Workflow steps ran with almost no tools.** `run_workflow_step` called
  `run_agent_turn` with no `extra_tools`/`extra_handlers` at all, so every step
  executed with only the two registry built-ins (`calculator`, `get_current_time`)
  no matter which tools its agent declared. All the real capabilities — document
  retrieval, Web IQ, memory recall — are *synthetic*: closure-bound to the
  authenticated user and therefore assembled per turn rather than registered, and
  that assembly existed only in the chat router. Nothing errored. The model simply
  answered that it could not read documents, search the web, or remember anything,
  and the run persisted that answer as a **successful** result — the reported
  symptom being a workflow that replied "I can't directly create persistent
  memories in the user account from this interface." Steps now receive the same
  shared capability set chat does, built once in `agents/capabilities.py` and
  injected identically by the in-request runner and the durable activity.
  `email` and `libraryDocumentIds` are frozen into the durable orchestration
  payload rather than re-read inside the activity, so a session edited mid-run
  cannot widen or narrow an in-flight run's document access.

- **Durable workflow execution had no way to reach it from the product.** The
  backend shipped, the paid Durable Task Scheduler was provisioned, the flag read
  `true` everywhere — and the web app's run payload never carried `durable`, so
  the scheduler sat idle and no caller could ever opt in. The capability was
  indistinguishable from a broken one, and nothing failed to say so. The workflow
  runner now offers **Keep running if the app restarts** (default off), and
  `GET /api/workflows` advertises `durableAvailable` derived from the *same*
  `app.state.durable_workflows` the run endpoint checks — so the advertisement
  cannot disagree with what the request will do. A separately-plumbed web-side
  flag was written first and deliberately reverted: a second copy of a feature's
  posture is the shadowing defect below, one layer up. Because a durable run
  answers `202` before the assistant turn exists, and the chat view loads a
  session's messages once without watching for later arrivals, the runner polls
  the run to a terminal state before handing off; a failed or still-running run
  keeps the user in the runner with a visible reason rather than dropping them
  into a session with no reply and no explanation.

- **Fourteen deploy-workflow exports silently shadowed their parameter-file defaults.**
  `${{ vars.X || 'fallback' }}` always expands to something non-empty, so azd never saw an
  empty value and never reached the `${X=default}` in `infra/main.parameters.json` — the
  workflow was a second, invisible source of truth for the same setting. Two had already
  drifted apart from the default they shadowed. This is what made the first attempt at
  enabling durable workflows inert: the parameter file said `true`, CI was green, the deploy
  was green, and `|| 'false'` pinned it off, so no scheduler was ever provisioned and
  nothing anywhere reported a problem. (`AI4IA_MEMORY_STORE` was the other, latent because
  the repo variable happens to be set — deleting that variable would have quietly disabled
  memory while the parameter file claimed `cosmos`.) The comment directly above the durable
  export already said not to add such a fallback, while the next line carried one. All
  fourteen removed; the parameter file now owns every default, since azd resolves an empty
  variable to it (proven live: `AI4IA_POSTGRES_LOCATION` is unset and has no fallback, and
  Postgres sits in the parameter default's `centralus`). Pinned by
  `test_no_azd_parameter_export_shadows_its_parameter_file_default`, which the existing
  reachability test could not catch — a shadowed token *is* exported, so it looks correctly
  plumbed.

- Three silently-swallowed failures in the API. `routers/tools.py` wrapped both MCP
  catalog-listing loops in a bare `except Exception: pass` and imported **no logger at
  all**, so a Cosmos read error made a user's MCP tools vanish from `GET /api/tools` with
  zero operator signal — the user simply concluded they had never been configured. Both
  now log and append a visible "unavailable" row, matching the module's own existing
  pattern for unresolvable tool metadata. `routers/inspector.py` dropped a document from
  the inspector list on a serialization error with no log, while the handler immediately
  above it logged correctly; it now logs too.
- The `owner` deployment guard had gone inert. `validate-feature-prereqs.py` rejected
  `ian-t-adams`, an older repo default, while `main.bicep` actually ships
  `ai4ia-operator` — so a deploy that never set `AI4IA_OWNER` tagged every resource with
  a placeholder and passed the check written to catch exactly that. Now warns on the
  value actually shipped (a warning, not an error, because `infra-validate` runs with no
  environment and legitimately resolves the default there).
- Stale documentation corrected: `docs/roadmap.md` listed proactive alerting as an open
  P1 when it has been live for some time (verified in Azure — action group
  `ag-ai4ia-slurmfactory` has a real recipient, both threshold rules are enabled, and the
  budget carries 50/80/100% notifications), and `app/web/README.md` pinned a Node floor
  and jsdom version that both contradicted `package.json`. The README now defers to
  `package.json`/`AGENTS.md` rather than restating versions that had already drifted once.
- Three infrastructure controls were configured but inert — legal ARM that deploys
  clean, renders normally in the portal, and does nothing. The $1,500/month resource-group
  budget notified **nobody**: `cost.bicep` builds an empty notifications map when
  `alertEmails` is empty, and `budgetAlertEmails` was never surfaced in
  `main.parameters.json`, so it stayed `[]` permanently (confirmed against live ARM —
  zero notifications on `budget-ai4ia-slurmfactory`). It now falls back to the already-wired
  `alertEmail`, and `validate-feature-prereqs.py` warns when neither is set. Key Vault
  purge protection had been described as "Set true for production" since `keyvault.bicep`
  was written, but `main.bicep` never passed the parameter, so no deployment could enable
  it; it is now reachable via `AI4IA_KEYVAULT_PURGE_PROTECTION` and deliberately defaults
  to `false`, because enabling it is irreversible and reserves the vault name for the
  soft-delete window. Wiring that knob also required a `deploy.yml` entry — the repo's own
  `test_every_azd_parameter_token_is_reachable_from_ci` caught the omission that would have
  made it a fourth inert knob.
- The Cosmos backup policy was never expressed in IaC, hiding how narrow recovery actually
  was: a 240-minute interval with **8-hour retention**, i.e. two snapshots, recoverable only
  by raising a support ticket. Now pinned in `infra/modules/data.bicep` and documented in the
  deployment runbook so the number is found before an incident rather than during one.

  **Superseded within the same unreleased cycle, and worth reading as a correction.** That
  work also recorded — in the Bicep comment, the runbook, and this changelog — that the
  account being **serverless** "rules out continuous backup and self-service point-in-time
  restore entirely" and that "Azure refuses to restore *into* a serverless account". Both
  claims were false. They were disproved empirically by creating a throwaway serverless
  account, enabling continuous backup on it, and restoring it: the restore succeeded and
  produced a **serverless** account (`createMode=Restore`) with the container and its
  partition key intact. The real serverless restriction belongs to Azure Backup **vaulted**
  backup (preview), which cannot restore to a serverless target — a different feature that is
  easy to conflate. This mattered more than a normal doc error, because the failure mode was
  to document the better posture as impossible: while it stood, no operator or agent would
  have attempted the upgrade.
- Documentation described the pre-Cosmos memory stack. The portal advertised "mem0 over
  Postgres + pgvector" and listed `mem0ai` and `psycopg` as dependencies, none of which the
  app still contains: zero `mem0`/`psycopg` imports in `app/api`, absent from `pyproject.toml`
  and `uv.lock`, and no pgvector store module exists. `configuration-reference.md` also told
  operators to set `PriorityWorker` — the singular name the proxy parses and silently
  discards — so following the table configured nothing. Postgres is described as what it now
  is (document-chunk fallback and legacy migration source) rather than removed.

- The browser-tab icon and five other brand rasters were never rebranded. The orange
  rebrand regenerated four assets; `scripts/gen-brand-assets.py` only ever wrote those
  four, so `app/web/src/app/favicon.ico` (the tab icon), `icon.png`, `apple-icon.png`,
  and all three `assets/branding/` files stayed on the previous azure mark. They kept
  their original dimensions and file sizes, so every check in `test_brand_assets.py` was
  structural and none could fail — and the six missing files were not even in its
  expectations table. The generator now owns all ten rasters (adding an Apple touch icon
  built full-bleed, since iOS applies its own mask), and the gate was rewritten to
  enumerate rasters from `git ls-files` and to decode actual pixels: assets must be
  ≥40% saturated pixels near the brand hue, which the current set clears at ~75% and the
  old azure mark scores 0% on. `scripts/tests/_pngread.py` is a small stdlib PNG decoder
  added so this runs in CI without Pillow. Saves a further 1.7 MB.

- Proxy priority reservations now actually reserve workers. `gateway.bicep` emitted the
  reservation as the container env var `PriorityWorker`, but the vendored proxy builds
  `PriorityWorkerDict` — the dictionary `WorkerFactory` reserves from — only from the plural
  `PriorityWorkers`. The singular name parsed, validated, and was discarded, leaving the
  dictionary at its `2:1,3:1` default: band 1, which admins resolve to, got **zero** reserved
  workers while every surface reported the feature enabled. Pinned from both sides.

- Custom accent colours now derive their own foreground from luminance instead of using a
  fixed per-theme `--accent-fg`. Five of the six shipped accent swatches previously failed
  WCAG AA in dark theme (worst case 2.9:1 white-on-indigo); the worst case is now 5.0:1.
  Three buttons that hardcoded `color: "#fff"` over `var(--accent)`, and one stale
  `var(--accent, #2563eb)` fallback, were fixed in the same pass.
- The portal's light colour scheme no longer renders dark text on the brand gradient at
  3.9:1 — the gradient foreground is now a scheme-aware `--on-brand` token rather than a
  hardcoded near-black, and the light-mode success/warning tokens were darkened to clear AA.

- APIM no longer treats every Foundry `400` as a temporary retryable error. Only
  `context_length_exceeded` remains retryable; malformed requests now return the
  provider's `400` without parking a healthy backend or producing `429 Requeue Message`.
- APIM permanent-error responses preserve the upstream 4xx body and `Content-Type`
  instead of returning `Content-Length: 0`.
- Responses API chat turns now send `"store": false`, because AI4IA stores conversation
  state in Cosmos and does not use `previous_response_id` chaining.
- The `deploy` job's `timeout-minutes: 60` was too low, and its failure mode was far worse
  than a failed run: GitHub reports a timed-out job as *cancelled*, which kills `azd`
  mid-provision while ARM keeps the in-flight deployment **active for up to 7 days**, so
  every later deploy fails validation with `DeploymentActive`. One timeout wedged the
  subscription and blocked the next four deploys. Raised to 180 minutes — the run that
  tripped it was an *incremental* provision, and the new-tenant cutover is a cold provision,
  which is strictly slower (APIM alone is 30–45 minutes). Added
  `docs/runbooks/deployment.md` §7.5 with the `az deployment group cancel` recovery.
- The toolbox provisioner did not model `toolbox_search` (`ToolSearchToolboxTool`), the
  thirteenth toolbox type added in `azure-ai-projects` 2.4.0, so the pinned-SDK bump could
  not land. Both it and the still-present `toolbox_search_preview` are now mapped, schema-
  branched, and covered by the example manifest.
- `gpt-5.4` and `gpt-5.4-pro` declared a 400,000-token context window; both are
  1,050,000. The understatement silently shrank the per-model max-output cap and the
  document context budget, so those models were being used well below their capability.
- The metadata tests in `test_per_model_metadata.py` pinned `gpt-5.4`'s context window as
  a literal, so correcting upstream model data failed tests that only cover serialization.
  They now assert against `infra/models.json`, which is exact and additionally catches
  build-time generator drift.
- `app-ci` was failing on every open PR. `ruff` was declared as `ruff>=0.5` with no
  explicit `select`, so 0.16.0's widened default rule set produced 498 errors with no
  source change. Pinned the rule set explicitly and bounded the version.
- `validate-feature-prereqs.py` required a non-empty `realtimeAllowedOrigins` outside
  `dev`, which would have failed every production and new-tenant deploy once the value
  became derived. It now rejects the opposite mistake: a literal hostname pinned in
  parameters.
- `infra-validate` ran `validate-catalog.py` and `validate-feature-prereqs.py` without
  listing either in its `paths` filter, so editing a validator skipped the job that runs it.
- `docs/runbooks/deployment.md`'s resource-provider guidance named 5 of the 18 namespaces
  an empty subscription needs, and its `az provider register -n A -n B` command was not
  valid — `-n` takes a single namespace, so it silently registered only the last.
- `docs/naming-and-tagging.md` presented `slurmfactory` as a fixed token in Foundry
  account/project names. Those are built from the azd environment name; the two only
  coincide because the current environment is named after the subscription.
- A comment in `app/api/pyproject.toml` claimed `azure-monitor-query` had been dropped
  "since only its metrics client was ever used (no logs client)". Both halves were false —
  `metrics/log_analytics.py` imports `LogsQueryClient` from it, and the dependency was
  still declared two lines below the comment saying it was gone. Acting on it would have
  removed a live dependency and broken the admin operations panel. The same wrong belief
  had propagated to `site/data/requirements.js`, which omitted the package entirely.
- `Sidebar.test.tsx` asserted `maxWidth: 100vw` / `maxHeight: 100dvh` via `toHaveStyle`.
  jsdom 30 drops viewport units from the CSSOM rather than storing them, so the values are
  no longer DOM-observable even though the component sets them and browsers honour them.
  Narrowed to the assertion that still describes behaviour.
- A 0-byte file named `'2026-07-31T21` committed by accident from a truncated shell
  redirect (Windows cannot use `:` in filenames).
- Two runbooks still described Speech Voice Live as blocked from production by a
  `403 AuthorizationFailed` on reading the subscription/resource group, with
  `docs/runbooks/deployment.md` instructing operators to **"stop and do not deploy."**
  The provider has been enabled in production since 2026-07-16
  (`AI4IA_SPEECH_VOICE_LIVE_ENABLED=true`), and the resource group reads normally, so
  the guidance contradicted live state. Both pages now record the gate as satisfied and
  keep only the rules that still stand (explicit approval for the live APIM policy
  compiler, zero-delete what-if for APIM changes, and the outstanding manual mic canary).

### Security

- `pypdf` raised past four CVEs, two HIGH. `CVE-2026-59935` / `CVE-2026-59936` are
  infinite loops in `page.extract_text()` during inline-image end-marker detection, which
  the document-extraction path reaches with user-supplied PDFs. The floor was raised in
  `pyproject.toml`, not just the lock, because nothing on the install path reads the lock.
- `postcss` raised to `^8.5.18` (GHSA-r28c-9q8g-f849, path traversal) and `sharp` pinned to
  `^0.35.0` via `overrides` (libvips advisories). `sharp` needed an override because
  `next` declares `optionalDependencies.sharp: ^0.34.5`, and a caret on a `0.x` version
  pins the minor — the range can never reach the fixed `0.35.0`. Verified in the resolved
  tree: only `sharp@0.35.3` is installed, with no nested copy under `next`.
- Dependabot security updates were enabled; the alert feed is now empty.

### Notes

- AI4IA is currently an active monorepo for a governed, multi-model Azure agentic chat app. Historical release entries should be backfilled only from reviewed tags or release notes.
