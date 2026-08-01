# Changelog

All notable changes to AI4IA will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses date-based release notes until formal versioning is introduced.

## [Unreleased]

### Added

- Root contributor guide, governance files, community templates, and third-party notices.
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

### Changed

- The Voice Live Origin allowlist is now **derived in Bicep** from the web app each
  deployment creates, instead of being a hardcoded hostname in
  `infra/main.parameters.json`. `AI4IA_REALTIME_ALLOWED_ORIGINS` only adds extra origins.
- Operator scripts (`teardown.ps1`, `purge-soft-deleted.ps1`, `status-snapshot.ps1`,
  `inventory.ps1`, `seed-models.ps1`) no longer carry tenant-specific defaults; each
  requires its target explicitly or resolves it from the selected azd environment.
- Curated and user-agent instruction sources are now visible read-only in the
  Conversation Inspector so inherited prompts can be audited without making those layers
  editable in the session panel.

### Fixed

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

### Notes

- AI4IA is currently an active monorepo for a governed, multi-model Azure agentic chat app. Historical release entries should be backfilled only from reviewed tags or release notes.
