# Changelog

All notable changes to AI4IA will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses date-based release notes until formal versioning is introduced.

## [Unreleased]

### Added

- Root contributor guide, governance files, community templates, and third-party notices.
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

### Fixed

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
