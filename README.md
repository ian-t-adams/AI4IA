# AI4IA

AI4IA is a governed, multi-model, multi-region **agentic chat workspace** and
Azure capability showcase for enterprise knowledge workers, agent builders, and
service administrators. It demonstrates durable conversations, models, tools,
documents, memory, voice, usage, and governance in one deployable monorepo.

The repository is not presented as a production-complete platform: current
capabilities and known gaps are documented below, and every real deployment
still needs tenant-specific security, identity, quota, data, and operational
review. [`PRODUCT.md`](PRODUCT.md) defines the users, purpose, and design
principles.

## Current state

Implemented: governed HTTP/SSE model traffic through SimpleL7Proxy -> APIM,
realtime through the FastAPI relay -> APIM, Entra/MSAL auth, agents
and workflows (with opt-in durable execution on Durable Task Scheduler, so a
run survives a deploy, scale-in, or crash), per-user memory, usage metering and
entitlements, voice/STT/TTS +
Voice Live, image/video generation, document and multimodal understanding,
custom remote MCP tools, admin usage/resource dashboards, and Azure Monitor /
Application Insights telemetry. Owner-visible execution receipts retain the
effective redacted prompt/context, memory and document provenance, tools
offered/invoked, bounded arguments/results, safety coverage, and correlation
metadata without claiming access to hidden model reasoning.

Advanced capabilities are feature-gated. The checked-in showcase profile keeps
the current broad capability set as environment-overridable defaults; a new
operator can opt out without editing the parameter file. For the observed
deployment posture, use the
[feature-enablement runbook](docs/runbooks/feature-enablement.md). For template
defaults and every deployable variable, use the
[configuration reference](docs/configuration-reference.md). Do not copy an
observed environment's values into a new tenant.

Some capabilities are deliberately incomplete, and the docs say so where it
matters: the Responsible AI record now carries the owner's non-blocking policy
decision, but assessment coverage, aggregate monitoring, disclosure, and
escalation remain incomplete across the full live modality scope
([decision record](docs/rai-decision-record.md)); memory has no per-user
capability switch ([memory](docs/memory.md)); and network isolation is design
scaffolding rather than a served private mode
([architecture](docs/architecture.md)).

## Repository layout

```text
/infra      Bicep modules, main.bicep, parameters, and model catalog
/app/web    Next.js web app: chat, admin dashboard, document/media UI, auth
/app/api    FastAPI backend: auth, chat, agents, tools, memory, documents, usage
/proxy      Vendored SimpleL7Proxy model gateway plus AI4IA Dockerfile/notes
/foundry    Toolbox, routine, and A2A manifests plus schemas
/scripts    Catalog, inventory, teardown, purge, and azd hook scripts
/site       Self-documenting portal (docs + timestamped status), published to GitHub Pages
/docs       Architecture, capability map, naming/tagging, and runbooks
/assets     Generated brand assets
azure.yaml  Azure Developer CLI service map
```

## Key decisions

- **IaC:** Bicep + Azure Developer CLI (`azd`).
- **Stack:** Next.js/TypeScript web, Python FastAPI API, .NET SimpleL7Proxy.
- **Model gateway:** compatible HTTP/SSE calls go SimpleL7Proxy -> APIM; realtime
  WebSockets go FastAPI relay -> APIM. Responses-API Code Interpreter Files and
  stateful sandbox calls bypass SimpleL7Proxy but use their own API-scoped APIM
  route; the FastAPI identity has no direct OpenAI inference role. Content
  Understanding, WebIQ grounding, and Azure Monitor are separate non-model
  control/data planes, not model inference.
- **Catalog-driven models:** `infra/models.json` is the deployment source of truth
  and generates the packaged API model catalog, including per-model
  `reasoning_effort` values.
- **Regions:** East US 2 and Sweden Central are the primary US/EU regions; West US
  carries targeted models such as MAI Image and deep research. See
  [`docs/region-capability-matrix.md`](docs/region-capability-matrix.md).
- **Identity:** Entra workforce/B2B now, with an internal user id decoupled from
  the identity provider.

## Deploy

Start with the **[guided Azure deployment](docs/runbooks/deploy-to-azure.md)**,
which is a multi-step production setup rather than a one-click template and routes into
the greenfield standup guide. It covers
cost/quota review, tools, deployment identity/RBAC, both GitHub OIDC subjects,
Entra apps, required variables, provider/model preflight, the first workflow
provision, data-plane assets, and custom-domain sequencing. Use the
[routine deployment runbook](docs/runbooks/deployment.md) for subsequent
exact-digest releases and rollback. A standalone `azd provision` is not the
release path for an existing environment because Bicep carries placeholder
images for greenfield creation.

If an AI coding agent is doing the work, point it at
**[deploying with a coding agent](docs/deploy-with-an-agent.md)**, which states
what the agent may do alone, what needs your approval, and the traps that cost
the most time.

## Documentation

The **[self-documenting portal](https://ian-t-adams.github.io/AI4IA/)** (published from
[`site/`](site/) to GitHub Pages) is the friendliest entry point: it explains the app,
renders the architecture diagrams, catalogues every deployed Azure service, lists the
requirements (IaC, permissions, packages), and shows a
**[timestamped status/health snapshot](https://ian-t-adams.github.io/AI4IA/status.html)** of the
deployed resources. The portal's **Docs** index, generated from
[`site/data/docs.manifest.json`](site/data/docs.manifest.json), is the complete
curated list. New operators should begin with the
[greenfield standup](docs/runbooks/greenfield-standup.md), then the
[configuration reference](docs/configuration-reference.md) and
[architecture](docs/architecture.md).

## Branding

![AI4IA lettermark](assets/branding/ai4ia-lettermark.png)

Brand assets live in [`assets/branding/`](assets/branding/), and are all generated
by `python scripts/gen-brand-assets.py` — never edit them by hand:

- `ai4ia-lettermark.png` — primary lettermark (1200x630, opaque). Byte-identical to
  the portal's Open Graph card so the two cannot drift.
- `ai4ia-icon-1024.png` — 1024x1024 icon with transparent rounded corners.
- `ai4ia-icon.ico` — multi-size Windows icon (16-256px).

The generator also owns the web app and portal icons. `scripts/tests/test_brand_assets.py`
fails if any committed image is not covered by it, or if one still carries the
previous palette.
