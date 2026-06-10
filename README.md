# AI4IA

A multi-model, multi-region **agentic chat application** for personal use and customer demos —
with all Azure infrastructure defined as code in this repository (monorepo).

It mirrors the Azure AI Foundry chat experience (pick a model, tune parameters, upload
documents, talk to it, edit system prompts) and adds multi-agent orchestration, workflows,
custom tools, per-user memory/history, theming + accessibility, and **governed model traffic**
through SimpleL7Proxy + APIM across multiple Foundry regions.

## Status
✅ **Phases 0–11 implemented.** Foundations + IaC, model gateway, web chat, agents & workflows
(Studio builder), per-user memory, usage/cost metering & entitlements, imagery, voice (STT/TTS +
agent-aware Voice Live), Entra/MSAL sign-in, and the document & multimodal understanding pipeline
(library, Content Understanding ingest, intent router + code-interpreter, audio/video grounding)
are all merged. Advanced capabilities ship **default-OFF** and are turned on per environment — see
[`docs/runbooks/feature-enablement.md`](docs/runbooks/feature-enablement.md). Architecture and the
phase arc: [`docs/architecture.md`](docs/architecture.md).

> **Deploying:** merges to `main` are validated by CI and (once the one-time OIDC setup is done)
> deployed by [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml). See the
> [deployment runbook](docs/runbooks/deployment.md).

## Repository layout
```
/infra      Bicep + main.bicep + models.json (azd targets)
/app/web    Next.js + TypeScript (chat UI, theming, a11y, @/ commands)
/app/api    Python FastAPI (auth, sessions, agents, memory, tools)
/app/agents Microsoft Agent Framework agents + workflows + custom tools
/proxy      SimpleL7Proxy model gateway + config
/scripts    teardown, purge, inventory, seed-models, azd hooks
/docs       architecture, region map, naming/tagging, runbooks
azure.yaml  azd service map
```

## Key decisions
- **IaC:** Bicep + Azure Developer CLI (`azd`).
- **Stack:** Next.js/TypeScript web + Python FastAPI API + Microsoft Agent Framework.
- **Model gateway from day one** — the backend always calls models through SimpleL7Proxy/APIM.
- **Data-driven catalog** — `infra/models.json` is the single source of truth for deployments,
  generated/validated from live Azure model availability.
- **Regions (v1):** East US 2 (US) + Sweden Central (EU) for full feature parity incl. voice;
  West US for MAI-Image-2.x and o3-deep-research. See
  [`docs/region-capability-matrix.md`](docs/region-capability-matrix.md).
- **Identity:** Entra MSAL (workforce + B2B guests now; External ID/CIAM later) with a canonical
  internal user ID decoupled from the IdP.

## Getting started (infra)
```powershell
az login
azd env new ai4ia-dev
azd up
```
> Validate in a parallel resource group before tearing down the existing stack —
> [`docs/runbooks/teardown.md`](docs/runbooks/teardown.md).

## Documentation
- [Architecture](docs/architecture.md)
- [Region & capability map](docs/region-capability-matrix.md)
- [Naming & tagging](docs/naming-and-tagging.md)
- [Deployment (CI/CD) runbook](docs/runbooks/deployment.md)
- [Feature enablement runbook](docs/runbooks/feature-enablement.md)
- [Teardown & rebuild runbook](docs/runbooks/teardown.md)

## Branding
![AI4IA lettermark](assets/branding/ai4ia-lettermark.png)

The mark above is the official **AI4IA** project lettermark. Brand assets live in
[`assets/branding/`](assets/branding/):

- `ai4ia-lettermark.png` — primary lettermark (1024×1024, opaque background).
- `ai4ia-icon-1024.png` — 1024×1024 icon with a transparent, rounded-corner background.
- `ai4ia-icon.ico` — multi-size Windows icon (16–256px) for app/favicon use.
