# AI4IA

AI4IA is a multi-model, multi-region **agentic chat application** for personal use
and customer demos. The repo is a monorepo: application code, model gateway, and
Azure infrastructure all live here and deploy through `azd`.

## Current state

Implemented: governed chat through SimpleL7Proxy + APIM, Entra/MSAL auth, agents
and workflows, per-user memory, usage metering and entitlements, voice/STT/TTS +
Voice Live, image/video generation, document and multimodal understanding,
custom remote MCP tools, admin usage/resource dashboards, and Azure Monitor /
Application Insights telemetry.

Advanced capabilities are feature-gated. Code defaults stay safe and mostly OFF;
the deployed environment is controlled by `infra/main.parameters.json` and azd
environment values. In the checked-in live parameters every capability is enabled,
including Web IQ search (the `/research` command) and the inline-attachment Code
Interpreter; Web IQ authenticates with the API's managed identity unless
`AI4IA_WEBIQ_API_KEY` is set. See
[`docs/runbooks/feature-enablement.md`](docs/runbooks/feature-enablement.md).

Known gaps to keep visible:

- The document library UI is still document-centric; custom analyzer authoring,
  folder-level sharing, and unauthenticated public links are not implemented.
- Memory has save/forget APIs and automatic recall, but no global user-facing
  memory toggle or recalled-memory indicator in chat.

## Repository layout

```text
/infra      Bicep modules, main.bicep, parameters, and model catalog
/app/web    Next.js web app: chat, admin dashboard, document/media UI, auth
/app/api    FastAPI backend: auth, chat, agents, tools, memory, documents, usage
/proxy      Vendored SimpleL7Proxy model gateway plus AI4IA Dockerfile/notes
/scripts    Catalog, inventory, teardown, purge, and azd hook scripts
/site       Self-documenting portal (docs + live status), published to GitHub Pages
/docs       Architecture, capability map, naming/tagging, and runbooks
azure.yaml  Azure Developer CLI service map
```

## Key decisions

- **IaC:** Bicep + Azure Developer CLI (`azd`).
- **Stack:** Next.js/TypeScript web, Python FastAPI API, .NET SimpleL7Proxy.
- **Model gateway:** backend model calls go through APIM/SimpleL7Proxy; direct
  Foundry calls are reserved for non-OpenAI control planes such as Content
  Understanding and Azure Monitor where required.
- **Catalog-driven models:** `infra/models.json` is the deployment source of truth
  and generates the packaged API model catalog.
- **Regions:** East US 2 and Sweden Central are the primary US/EU regions; West US
  carries targeted models such as MAI Image and deep research. See
  [`docs/region-capability-matrix.md`](docs/region-capability-matrix.md).
- **Identity:** Entra workforce/B2B now, with an internal user id decoupled from
  the identity provider.

## Deploy

```powershell
az login
azd env new ai4ia-dev
azd up
```

Merges to `main` deploy only after the one-time GitHub OIDC setup is complete.
See [`docs/runbooks/deployment.md`](docs/runbooks/deployment.md).

## Documentation

The **[self-documenting portal](https://ian-t-adams.github.io/AI4IA/)** (published from
[`site/`](site/) to GitHub Pages) is the friendliest entry point: it explains the app,
renders the architecture diagrams, catalogues every deployed Azure service, lists the
requirements (IaC, permissions, packages), and shows a
**[live status/health view](https://ian-t-adams.github.io/AI4IA/status.html)** of the
deployed resources. The authoritative Markdown lives here:

- [User guide](docs/user-guide.md)
- [Architecture](docs/architecture.md)
- [Editable architecture visual](docs/architecture-overview.excalidraw)
- [Region & capability map](docs/region-capability-matrix.md)
- [Naming & tagging](docs/naming-and-tagging.md)
- [Configuration reference](docs/configuration-reference.md)
- [Brutal repo audit](docs/brutal-audit.md)
- [Deployment runbook](docs/runbooks/deployment.md)
- [Feature enablement runbook](docs/runbooks/feature-enablement.md)
- [Teardown & rebuild runbook](docs/runbooks/teardown.md)
- [Document & multimodal understanding](docs/document-multimodal-understanding.md)

## Branding

![AI4IA lettermark](assets/branding/ai4ia-lettermark.png)

Brand assets live in [`assets/branding/`](assets/branding/):

- `ai4ia-lettermark.png` — primary lettermark (1024x1024, opaque background).
- `ai4ia-icon-1024.png` — 1024x1024 icon with transparent rounded corners.
- `ai4ia-icon.ico` — multi-size Windows icon (16-256px).
