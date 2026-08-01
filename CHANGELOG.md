# Changelog

All notable changes to AI4IA will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses date-based release notes until formal versioning is introduced.

## [Unreleased]

### Added

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

### Removed

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

- The Voice Live Origin allowlist is now **derived in Bicep** from the web app each
  deployment creates, instead of being a hardcoded hostname in
  `infra/main.parameters.json`. `AI4IA_REALTIME_ALLOWED_ORIGINS` only adds extra origins.
- Operator scripts (`teardown.ps1`, `purge-soft-deleted.ps1`, `status-snapshot.ps1`,
  `inventory.ps1`, `seed-models.ps1`) no longer carry tenant-specific defaults; each
  requires its target explicitly or resolves it from the selected azd environment.
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
