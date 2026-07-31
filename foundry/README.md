# `foundry/` — Foundry Agent Service toolbox + skills (ACTIVATED, preview)

This directory holds the **declarative, operator-owned** definition of the single Azure AI
Foundry **Agent Service toolbox** that AI4IA fronts through its existing official-MCP APIM.
`toolbox.manifest.json` is the canonical `ai4ia-toolbox` definition (live in the primary project);
the toolbox is a data-plane resource created by the provisioning scripts (not `azd up`), and the
feature flags are on in `infra/main.parameters.json`.

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
| `toolbox.manifest.json` | Canonical definition of the live `ai4ia-toolbox` (a new tenant reproduces it 1:1). |
| `toolbox.manifest.example.json` | Populated reference manifest with one of every tool (copy-paste starting point). |
| `toolbox.manifest.schema.json` | JSON Schema for the manifest; validated in CI (`infra-validate`). |
| `skills/<name>/SKILL.md` | Agent-Skills packages (YAML front matter + Markdown instructions) bound to the toolbox. |
| `routines/routine.schema.json` + `routines/example.routine.json` | Routine schema + a populated example; a routine's tool calls flow through the APIM-fronted toolbox. |
| `a2a/a2a.schema.json` + `a2a/example.a2a.json` | A2A exposure schema + example; fronts a deployed agent's A2A endpoint through APIM. |

## Provisioning (operator, one time)

```bash
# 0. Install the provisioning-only extra (CI's app-ci.yml api job installs it too, so the
#    real-SDK toolbox construction tests run there; the runtime container never does).
uv pip install -e "app/api[foundry]"

# 1. Create skills (dry run first; --create writes to Foundry).
python scripts/provision-foundry-skills.py
python scripts/provision-foundry-skills.py --create

# 2. Populate toolbox.manifest.json (tools + skills), then create the toolbox.
#    Tip: copy toolbox.manifest.example.json (one of every tool) as a starting point.
python scripts/provision-foundry-toolbox.py            # dry run: prints plan + mcp-servers.json entry
python scripts/provision-foundry-toolbox.py --create

# 3. Paste the printed entry into infra/mcp-servers.json, set
#    enableOfficialMcp=true + enableFoundryToolbox=true, and `azd up`.

# 4. (optional) Inventory the APIM-fronted MCP servers in an Azure API Center
#    private tool catalog (set enablePrivateToolCatalog=true first).
python scripts/provision-private-tool-catalog.py     # dry run: prints APIM URLs to catalog
python scripts/provision-private-tool-catalog.py --create

# 5. (optional) Validate a routine and see its plan. Its tool calls target the toolbox, so
#    they inherit the APIM bridge. Edit foundry/routines/example.routine.json first.
#    There is no --create: azure-ai-projects 2.4.0's routines surface (an event-trigger model)
#    cannot faithfully represent this steps-based workflow shape -- see docs/foundry-toolbox.md.
python scripts/provision-foundry-routine.py          # validates + prints steps and tool calls

# 6. (optional) Expose a deployed agent over A2A, fronted through APIM. Edit
#    foundry/a2a/example.a2a.json, then emit the enable + APIM-front commands.
python scripts/provision-foundry-a2a.py              # dry run: prints URLs + agents.json stub
python scripts/provision-foundry-a2a.py --emit-az    # prints the `az` enable + APIM commands
```

The scripts default to a **safe offline dry run** and read the project endpoint from
`--project-endpoint` or the `AZURE_FOUNDRY_PROJECT_ENDPOINT` azd output (emitted when
`enableFoundryToolbox=true`). Everything here is **public preview**; do not use in production
without your own validation.
