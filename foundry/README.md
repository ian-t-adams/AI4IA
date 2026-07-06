# `foundry/` — Foundry Agent Service toolbox + skills (default-OFF, preview)

This directory holds the **declarative, operator-owned** definition of the single Azure AI
Foundry **Agent Service toolbox** that AI4IA fronts through its existing official-MCP APIM.
Nothing here is provisioned by `azd up`, CI, or the app runtime; it ships **inert** and is
created only when an operator runs the provisioning scripts and flips the feature flags.

Full design + runbook: [`docs/foundry-toolbox.md`](../docs/foundry-toolbox.md).

## The bridge, in one line

A Foundry toolbox *is itself an MCP endpoint*
(`{project_endpoint}/toolboxes/{name}/mcp?api-version=v1`). We register that endpoint as one
"official MCP server" in `infra/mcp-servers.json`, so the app consumes the whole toolbox —
web/AI search, code interpreter, tool search, skills — through the APIM front door shipped in
PR #125, with **zero new runtime code**. APIM injects the managed-identity bearer, the static
`Foundry-Features: Toolboxes=V1Preview` header, and the `api-version=v1` query.

## Files

| Path | What it is |
| --- | --- |
| `toolbox.manifest.json` | The toolbox definition (tools/skills/connections). Ships **empty** on purpose. |
| `toolbox.manifest.schema.json` | JSON Schema for the manifest; validated in CI (`infra-validate`). |
| `skills/<name>/SKILL.md` | Agent-Skills packages (YAML front matter + Markdown instructions) bound to the toolbox. |

## Provisioning (operator, one time)

```bash
# 0. Install the provisioning-only extra (not in the runtime container / CI).
uv pip install -e "app/api[foundry]"

# 1. Create skills (dry run first; --create writes to Foundry).
python scripts/provision-foundry-skills.py
python scripts/provision-foundry-skills.py --create

# 2. Populate toolbox.manifest.json (tools + skills), then create the toolbox.
python scripts/provision-foundry-toolbox.py            # dry run: prints plan + mcp-servers.json entry
python scripts/provision-foundry-toolbox.py --create

# 3. Paste the printed entry into infra/mcp-servers.json, set
#    enableOfficialMcp=true + enableFoundryToolbox=true, and `azd up`.
```

The scripts default to a **safe offline dry run** and read the project endpoint from
`--project-endpoint` or the `AZURE_FOUNDRY_PROJECT_ENDPOINT` azd output (emitted when
`enableFoundryToolbox=true`). Everything here is **public preview**; do not use in production
without your own validation.
