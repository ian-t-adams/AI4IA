# AI4IA

AI4IA is a governed AI workspace for conversations, agents, documents, memory,
voice, and generated media. Users can choose among catalogued models without
moving their history or granting each provider direct access to their tools.
The application makes effective settings, tool permissions, execution evidence,
and usage visible alongside the work.

It is also an **Azure architecture showcase**, not a production-complete
platform. Its value is the integration: identity, model gateways, durable data,
retrieval, orchestration, and observability working together with explicit
boundaries and documented limitations.

## Start here

| Goal | Read |
| --- | --- |
| Use chat, agents, documents, voice, and tools | [User guide](docs/user-guide.md) |
| Understand the design and its Azure tradeoffs | [Architecture](docs/architecture.md) |
| Understand regions, processing location, and model capacity | [Region and capability map](docs/region-capability-matrix.md) |
| Deploy into your own tenant | [Guided Azure deployment](docs/runbooks/deploy-to-azure.md) |
| Change code or contribute a capability | [Contributor guide](AGENTS.md) |
| Browse the complete documentation and dated deployment status | [Documentation portal](https://ian-t-adams.github.io/AI4IA/) |

## What the workspace does

Use plain chat for one-off questions, an agent for a reusable persona and tool
bundle, or a workflow for repeatable steps. Documents can be temporary chat
attachments or reusable library sources. Memory carries selected personal
context between conversations. Voice shares the conversation rather than
creating a separate work surface.

Tools can search the web, read documents, run governed sandbox computations, and
create artifacts when the environment enables those capabilities. External
calls may require approval. Optional session/run auto-approval is explicit,
bounded, and revocable; it does not grant new tools or bypass authorization.
Execution receipts record what was supplied and executed, **not hidden model
reasoning**.

## How it fits together

```text
Browser -> Next.js -> FastAPI -> SimpleL7Proxy -> APIM -> Foundry
                        |
                        +-> Cosmos: records and memory text/vectors
                        +-> Blob: source documents and generated artifacts
                        +-> AI Search: rebuildable document retrieval
```

FastAPI owns identity, user isolation, feature gates, tool authorization, and
usage. SimpleL7Proxy owns HTTP/SSE queueing and delayed retries; API Management
owns model routing, bounded regional attempts, and managed-identity access to
Foundry. Models and generated gateway routes come from `infra/models.json`.

Two model paths bypass **only SimpleL7Proxy**, never APIM: realtime WebSockets
use the FastAPI relay, and Code Interpreter uses its own constrained APIM API.
Native service data planes, including Cosmos, Blob, Search, Content
Understanding, and WebIQ, are separate from this model-inference path.

## Design implications

- **Multi-region models are not a multi-region application.** The gateway,
  application, and canonical data have their own availability boundaries.
  Selecting an EU model does not relocate saved conversations or documents.
- **Durable data is not disposable infrastructure.** Cosmos records and Blob
  source bytes are canonical; rebuilding a search index is different from
  recovering deleted user data.
- **An enabled feature is not evidence that every endpoint works.** Provider
  entitlement, model availability, credentials, and runtime prerequisites still
  apply. Template defaults and live observations are recorded separately.
- **Unknown means unknown.** Missing usage, cost, telemetry, or safety
  assessments must not be shown as zero, healthy, or safe.

The [Responsible AI record](docs/rai-decision-record.md) records the owner's
non-blocking assessment policy and remaining coverage/monitoring gaps. There is
no served private-network mode and no per-user memory opt-out switch. These are
limitations, not capabilities implied by the showcase.

## Run or deploy

For local development, use the [API](app/api/README.md#local-dev) and
[web](app/web/README.md#local-dev) instructions. Local identity and in-memory
stores support UI development; model responses still require a configured
gateway.

Azure deployment is a staged setup involving quota, identity, RBAC, configuration,
and data-plane assets. Start with the [deployment guide](docs/runbooks/deploy-to-azure.md);
use the [routine release runbook](docs/runbooks/deployment.md) for later releases.
The release workflow builds each image once and deploys its registry digest.
A standalone `azd provision` is not an application release and can temporarily
restore greenfield placeholder images.

Coding agents must also follow [the agent deployment rules](docs/deploy-with-an-agent.md).
New resources, privilege changes, destructive operations, and deployments require
the owner's approval.

## Repository

| Path | Responsibility |
| --- | --- |
| `app/web` | Next.js/React experience and same-origin HTTP proxy |
| `app/api` | FastAPI application, governance, integrations, and stores |
| `infra` | Bicep, deployable parameters, model/tool/voice catalogs, APIM policies |
| `proxy` | Pinned SimpleL7Proxy source and the maintained integration |
| `foundry` | Toolbox and skill manifests; clearly labelled design-only examples |
| `scripts` | Generation, validation, release, inventory, and recovery tooling |
| `docs` / `site` | Explanatory documentation, operator runbooks, and static portal |

The [configuration reference](docs/configuration-reference.md) owns deployment
settings; the [feature runbook](docs/runbooks/feature-enablement.md) separates
defaults from observed posture. The portal's [status page](https://ian-t-adams.github.io/AI4IA/status.html)
is timestamped evidence, not a real-time availability guarantee.
