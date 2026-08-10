# `foundry/` — Foundry Agent Service toolbox (ACTIVATED, preview)

This directory holds the **declarative, operator-owned** definition of the single Azure AI
Foundry **Agent Service toolbox** that AI4IA fronts through its existing official-MCP APIM.
`toolbox.manifest.json` is the canonical `ai4ia-toolbox` definition (live in the primary project);
the toolbox is a data-plane resource created by the provisioning scripts (not `azd up`), and the
feature flags are on in `infra/main.parameters.json`.

Full design + runbook: [`docs/foundry-toolbox.md`](../docs/foundry-toolbox.md).

## The bridge, in one line

A Foundry toolbox *is itself an MCP endpoint*
(`{project_endpoint}/toolboxes/{name}/mcp?api-version=v1`). We register that endpoint as one
"official MCP server" in `infra/mcp-servers.json`, so the app consumes the toolbox's tools —
web/AI search, code interpreter, and tool search — through the APIM front door shipped in
PR #125, with **zero new runtime code**. APIM injects the managed-identity bearer, the static
`Foundry-Features: Toolboxes=V1Preview` header, and the `api-version=v1` query.

## Files

| Path | What it is |
| --- | --- |
| `toolbox.manifest.json` | Canonical definition of the live `ai4ia-toolbox` (a new tenant reproduces it 1:1). |
| `toolbox.manifest.example.json` | Populated `reference` manifest with one of every tool. It is never reconciled as-is. |
| `toolbox.manifest.schema.json` | JSON Schema for the manifest; validated in CI (`infra-validate`). |
| `routines/routine.schema.json` + `routines/example.routine.json` | Design/preview routine contract. It is validated against canonical toolbox names but is not created or served. |
| `a2a/a2a.schema.json` + `a2a/example.a2a.json` | Design/preview A2A contract with an explicit blocker inventory. It does not create a callable integration. |

## Reconciliation

`.github/workflows/foundry-assets.yml` uses GitHub OIDC to reconcile the **toolbox**
data-plane asset. Routine and A2A files are validation-only and are never reconciled.
The workflow is **not** triggered directly by changes under `foundry/**`:
it runs on `workflow_run` after the `deploy` workflow completes successfully for a
push to `main`, and on manual dispatch. That ordering is the point — `deploy` is
what provisions the project-scoped Foundry User role, so reconciling in parallel
with (or ahead of) it would race RBAC creation and propagation. `foundry/**` is
included in `deploy`'s push paths, so editing a manifest still reaches
reconciliation; it just arrives after the infrastructure it depends on. A successful
deploy retains its azd-produced project endpoint for 30 days. An unprivileged gate
downloads that exact run's artifact before the protected reconciliation job can
request OIDC, then carries the validated endpoint as a job output while approval or
concurrency waits. Manual dispatch requires the operator to pass the current azd
output explicitly. A fail-closed preflight requires the OIDC identity
to hold the project-scoped Foundry User role before reconciliation. Failures stop
the workflow; unchanged defaults create no immutable versions.

For a local/operator run:

```powershell
# 0. Install the exact provisioning-only extra from the repository root.
#    app-ci installs this too; the runtime container never does.
python -m pip install -e "app/api[foundry]"

# 1. Export the azd-produced project endpoint into this shell.
$projectEndpoint = azd env get-value AZURE_FOUNDRY_PROJECT_ENDPOINT
$env:AZURE_FOUNDRY_PROJECT_ENDPOINT = $projectEndpoint

# 2. Verify the caller has project-scoped Foundry User data-plane access.
python scripts/provision-foundry-toolbox.py --check-access

# 3. Populate toolbox.manifest.json, then inspect the reconciliation plan.
#    Tip: copy toolbox.manifest.example.json, prune/review it, and change
#    lifecycle from reference to active before any approved reconciliation.
python scripts/provision-foundry-toolbox.py            # dry run: prints plan + mcp-servers.json entry

# 4. Paste the printed entry into infra/mcp-servers.json, set
#    enableOfficialMcp=true + enableFoundryToolbox=true, and use the deploy workflow.

# 5. Validate design-only artifacts. Neither command creates or serves anything.
python scripts/provision-foundry-routine.py --check
python scripts/provision-foundry-a2a.py --check

# 6. Manual repair only: pass the selected azd environment's endpoint explicitly.
#    Automatic post-deploy reconciliation consumes the triggering deploy's artifact.
gh workflow run foundry-assets.yml --ref main -f project_endpoint=$projectEndpoint
```

The toolbox script reads the project endpoint from `--project-endpoint` or
`AZURE_FOUNDRY_PROJECT_ENDPOINT` (an azd output when `enableFoundryToolbox=true`).
For a first standup, the workflow is authoritative because it runs after deploy
has created the OIDC identity's project role. Reserve a local `--create` for an
explicitly approved repair after confirming the local operator has that same role.
Routine and A2A scripts are offline design validators. Everything here is public
preview; do not infer that a schema-valid routine or A2A design is callable.
