# Changelog

Notable changes to AI4IA, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format.

The project has no tagged releases yet, so everything lives under **Unreleased**.
Merging, deploying, and tagging are three separate events; an entry here records a
repository change, not a claim that it is deployed.

## [Unreleased]

### Added

- **Per-turn execution receipts** — every persisted model turn now shows the
  effective redacted prompt/context, instruction and agent-configuration hashes,
  admitted/displaced memory and document source versions, tools offered versus
  invoked, bounded redacted arguments/results, approvals, safety coverage,
  resolved deployment metadata, and correlation id. Receipts explicitly exclude
  hidden chain-of-thought and retain digests/byte counts when payloads are capped.
- **Progressively disclosed Foundry skills** — the official Toolbox now
  discovers curated `skill://` MCP resources and offers a bounded `load_skill`
  function. Full instructions load only on selection, with source, content hash,
  version/default resolution, and truncation provenance retained for execution
  receipts. `evidence-review` is the first repository-owned skill, and the
  Foundry asset provisioner idempotently reconciles immutable skill versions
  before the toolbox.
- **Data residency control** (`AI4IA_DATA_RESIDENCY`) — a `global` / `zonal` /
  `us` / `eu` ladder enforced on `app.state.catalog`, the single point every route
  resolves a deployment through. Residency derives from the deployment **SKU**,
  not endpoint geography: a `GlobalStandard` deployment in Sweden Central is not
  EU-resident. Startup refuses rather than silently disabling an enabled feature
  whose model the policy excludes.
- **Content-safety annotations** — per-category verdicts returned by chat
  completions and the Responses API are normalized, persisted, and shown with
  severity ordinals. Multi-iteration agent turns retain each model-call
  assessment; missing coverage is explicit. The deployment policy also enables
  indirect-attack assessment without blocking. This does not yet provide
  artifact-level coverage for image, video, or Voice Live modalities.
- **Durable workflow execution** — `POST /api/workflows/{name}/run` accepts an
  opt-in `"durable": true` that runs the workflow on an Azure Durable Task
  Scheduler orchestration, so a run survives a deploy, scale-in, or crash.
- **Workflow runner UI** — running a workflow now shows its result, and each step
  declares the capabilities it may use.
- **Per-step tool grants for workflows** — a step receives only the tools it was
  granted, instead of inheriting the whole catalog.
- **Shared agent capabilities** and a `remember_memory` tool, both selectable from
  the agent builder.
- **Resource-provider preflight** in the deploy workflow, deriving required
  namespaces from `infra/**/*.bicep` and registering them at subscription scope.
- **`scripts/capture-data-recovery-state.ps1`** — records the Cosmos restore
  coordinates, blob manifest, and Key Vault secret names that stop being knowable
  once the resource group is deleted.

### Changed

- **Responses-API Code Interpreter now stays behind APIM.** `run_code` and
  `analyze_attachment` use a dedicated API-scoped subscription on the existing
  Basic v2 APIM. Policy fixes the model, requires `store=false` plus exactly one
  Code Interpreter tool, preserves multipart Files bodies, strips caller
  credentials, and authenticates to the primary Foundry account with managed
  identity. The FastAPI UAMI no longer receives account-wide OpenAI inference
  permission.
- **APIM child entity names derive from `${workload}`** rather than a literal
  `ai4ia-*` prefix. APIM children share one flat namespace per service, so
  hardcoded product and subscription names meant a second workload on the shared
  gateway would adopt and overwrite this app's credentials instead of failing.
  `workload` gained an `${AI4IA_WORKLOAD=ai4ia}` override, and the deploy workflow
  derives its resource group from the same token. The default resolves
  byte-identically to the previous names.
- **Theme-aware status and accent foregrounds.** A foreground colour is derived
  from the accent it sits on rather than fixed per theme, so a user-chosen accent
  stays legible. Status colours use tokens instead of literals.
- **Region and data-zone constraints no longer relax silently** when a requested
  model is unavailable in the selected scope.
- **The proxy flushes each SSE event** instead of buffering, so streamed tokens
  arrive as they are produced.

### Removed

- **The legacy Consumption-tier APIM** and every child resource — its
  model/realtime APIs, policies, policy fragments, subscriptions, diagnostics,
  Foundry role assignments, the unused `legacyConsumptionApimGatewayUrl` output,
  and the legacy realtime routing policy. It existed only as an HTTP/SSE rollback
  plane during the Basic v2 migration. `gateway.bicep` now attaches its children to
  the existing Basic v2 service, the only APIM in the environment. Realtime never
  had a Consumption rollback path, because Consumption does not support WebSockets.
- **PostgreSQL Flexible Server**, retired after the memory migration to Cosmos
  completed. Cosmos is the only memory backend and Azure AI Search the only
  document-chunk index. `scripts/tests/test_postgres_retired.py` fails if
  provisioning returns.
- **Dead symbols**, each confirmed by a repo-wide search returning only its own
  definition: `revokeDocumentShare` (`app/web/src/lib/api.ts`), `clear_quarantine`,
  `health_status` and `McpHealthStatus` (`agents/mcp_health.py`), `telemetry_enabled`
  (`logging_setup.py`), `ErrorResponse` (`errors.py`), `VoiceProviderSelectionMode`,
  the TypeScript `AzureOpenAIVoiceProvider` type, and the unused `environmentName`
  parameter in `web.bicep`. The server endpoint
  `DELETE /documents/{id}/shares/{email}` was deliberately kept: it is public,
  tested API surface, and removing it is a breaking change needing its own review.

### Fixed

- **Sharing no longer revokes an ACL when a read fails.** A transient read error
  was treated as an empty grantee list.
- **Durable workflow runs no longer drop per-step fields** when rehydrating an
  orchestration.
- **Deploy-workflow exports no longer shadow parameter-file defaults.** Fourteen
  `AI4IA_*` exports set an empty value that silently overrode the checked-in
  default.
- **The portal no longer presents stale evidence as live health.** A failed status
  refresh stops publication rather than republishing an old snapshot.

### Security

- **Every first-party capability is behind the tool-approval gate**
  (`agents/synthetic_governance.py`). Approval previously read risk off a
  `ToolSpec`, and only registry tools have one — but the capabilities that reach
  the network, spend money, execute code, or write durable state are dispatched
  before the registry path, so the gate could not see them. `browse_url`, an
  arbitrary-URL fetch whose destination a poisoned document can name, was the live
  consequence.

  Each capability now carries a real spec, so one definition of risk serves both
  dispatch routes. `browse_url`, `run_code`, and `analyze_attachment` are held on
  **every** turn, because the model chooses the destination, the program, or the
  content. Web search, image/video generation, `remember_memory`, and
  `export_document` are held only on a turn that carried untrusted content: their
  destination is fixed by server configuration and their effect is confined to the
  caller's own data. That relaxation is declared per tool
  (`ToolSpec.injection_only_risk`), is not operator-configurable, and never weakens
  a call below `tainted` strength. A synthetic capability with no classification is
  refused rather than run, so a new capability cannot acquire an execution route
  without also acquiring a risk.

- **The proxy-ingress credential is no longer emitted** in deployment output, and
  the key was rotated. [`docs/runbooks/key-rotation.md`](docs/runbooks/key-rotation.md)
  covers zero-downtime rotation using the proxy's dual-key accept.

- **Identity-only authentication on the Foundry accounts.** `disableLocalAuth` is
  true, so an account key cannot be used to reach a deployment directly and skip
  APIM's rate limiting, residency policy, usage metering, and priority routing.

- **Teardown requires a separate data-loss acknowledgement** before it will delete
  resources holding user data.
