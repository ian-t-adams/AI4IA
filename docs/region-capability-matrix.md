# Region and capability map

`infra/models.json` is the **desired deployment catalog**, not a live inventory.
It owns models, per-deployment versions, regions, SKUs, capacities, provider
protocols, and advertised capabilities. Generated application catalogs and APIM
policies follow it. This page explains how to interpret those choices rather
than maintaining a second model/version list.

## Region strategy

| Region | Role | Consequence |
| --- | --- | --- |
| East US 2 | Primary US model set, realtime/audio, images/video, and native Content Understanding/Speech integration | Broadest shared capability set; several native service dependencies remain here |
| Sweden Central | Second primary model region, with EU data-zone options where the catalog offers them | Useful backend diversity, but not every model or SKU has parity |
| West US | Targeted MAI image and deep-research deployments | Availability/quota specialization rather than a complete third copy |

The application, APIM, storage, search, and telemetry have their own locations.
Multiple Foundry regions do **not** create a multi-region application or a
replicated user-data tier.

## Data-zone semantics

Keep these three locations separate:

1. **Resource location:** where the account/deployment is created.
2. **Inference processing boundary:** determined by the model's deployment SKU.
3. **Application storage and other service processing:** Cosmos, Blob, Search,
   telemetry, document analysis, and external tools have separate boundaries.

| Catalog SKU | Processing implication |
| --- | --- |
| `GlobalStandard` | Processing can be globally routed; a regional account name does not constrain it to that region |
| `DataZoneStandard` | Processing stays within Microsoft's specified data zone; the US/EU options exposed by this catalog follow that boundary |
| `Standard` | Geography-based processing, subject to the provider's documented deployment contract |

In particular, a Sweden Central **Global Standard** model is not an EU-only
option. An EU-bounded model call also does not relocate a conversation or its
documents from storage in another geography. A residency requirement must cover
the complete request path, including retrieval, memory, native analysis, and
outbound tools.

Microsoft's [deployment-type documentation](https://learn.microsoft.com/azure/ai-services/openai/how-to/deployment-types)
is authoritative for processing semantics. AI4IA's
[configuration reference](configuration-reference.md#data-residency) describes
the subset enforced by its model-routing policy.

## Capacity is not capability

The catalog separates portable `capacity` from optional `maxCapacity`:

- **Baseline** uses the portable allocation.
- **Maximum** uses subscription-specific values produced by
  `scripts/sync-model-capacity.py`, falling back to baseline where none exists.

Maximum is not an instruction to allocate the full displayed quota in every
region. Quota pools can be regional, global, or shared across the subscription;
the model's capacity unit can represent tokens or requests rather than a common
unit across providers. A maximum profile can leave no headroom for another
deployment or validation environment.

MAI image deployments illustrate the distinction: the configured subscription
uses single-region allocations because the relevant model pools are
subscription-wide. Creating a second resource group does not create a second
quota pool. Change the topology only after fresh availability/capacity evidence
and an explicit allocation decision.

Capacity limits throughput. It does not grant an upstream entitlement, make a
model tool-capable, enable an unsupported API, or guarantee low latency.

Use the [read-only capacity evidence report](runbooks/deploy-to-azure.md#read-only-capacity-and-usage-evidence)
to collect dated live allocations, raw quota/platform observations and bounded
per-deployment request/token counts. It keeps measured zero distinct from missing
samples, never sums unlike units, and does not infer pool identity from processing
geography or the catalog's maximum declarations. Optional reviewed pool
assertions are explicitly operator-supplied, not authorization to change capacity.
No production-critical selection, reserve policy or production profile is implied.

## Model capabilities and provider paths

Models are not interchangeable simply because they accept text.

| Surface | What must agree |
| --- | --- |
| Plain chat | Input modalities, context/output limits, supported sampling and reasoning values |
| Agents and workflows | The model must support the tool contract; plain-chat-only models are rejected for tool-dependent execution |
| Images | Provider API, supported sizes/quality, output bounds, safety behavior, and cost coverage |
| Video | Asynchronous create/status/content contract, supported clip lengths, and durable delivery |
| Document parsing | Explicit analyzer selection, page/byte limits, canonical extraction format, and page-based metering |
| Realtime | Provider/model allowlist, audio contract, separate APIM WebSocket API, and voice-capable tools |

Responses and Anthropic adapters translate the agent loop's internal format;
they still use SimpleL7Proxy and APIM. MAI and other provider-native media routes
are generated from the catalog rather than guessed OpenAI endpoints. A catalog
row alone is not a functioning integration.

Advertised reasoning-effort values reflect the app's supported, exercised
surface, not every value mentioned by provider documentation or an error
message. A new value needs provider-path evidence; deployment success alone
does not establish parameter support.

Image models without an unambiguous mapped Azure meter remain **cost-unknown**.
Token/page estimates live in `app/api/src/ai4ia_api/data/pricing.json` and are not
authoritative billing. Context length, caching, service tier, and meter changes
can make a simple estimate differ from the bill.

## Speech Voice Live managed-model catalog

Speech Voice Live is separate from Azure OpenAI realtime deployments.
`infra/voice-providers.json` owns its curated East US 2 managed models, voices,
capabilities, and stable `2026-04-10` API contract. Azure OpenAI remains the
default provider; enabling Speech requires its own allowlist, APIM API/key,
and managed-identity access.

| Managed model | Response path | Input transcription |
| --- | --- | --- |
| `gpt-realtime` (default) | Native audio | `gpt-4o-transcribe` |
| `gpt-realtime-mini` | Native audio | `gpt-4o-transcribe` |
| `gpt-4.1` | Azure Speech chain | `azure-speech` |
| `gpt-4.1-mini` | Azure Speech chain | `azure-speech` |
| `gpt-5-mini` | Azure Speech chain | `azure-speech` |
| `gpt-5.1` | Azure Speech chain | `azure-speech` |

A Speech-managed model name is not a promise that an identically named normal
chat deployment exists. Extending Speech to another region is a separate
catalog, gateway, and permission change, not a browser preference.

## Lifecycle and change discipline

Three independent checks are needed before a model change:

| Check | What it establishes | What it does not establish |
| --- | --- | --- |
| Source/schema/generated-catalog checks | Internally consistent names, routes, and capabilities | Subscription access or live provider behavior |
| Subscription preflight | Offering, lifecycle, and required capacity in the selected subscription | Successful application/tool execution |
| Governed application exercise | The actual adapter, gateway, parameters, and workflow behave together | Future availability or every possible workload |

Every deployment pins its version and disables automatic upgrade. A
deprecating model may continue serving an existing deployment while refusing a
new one; an available quota counter may refer to fine-tuning rather than
inference. Preview offers can also expire. Recheck before a new standup, a
capacity change, or a model retirement rather than copying an old capacity table.

Use `scripts/check-model-availability.py` for the subscription preflight and
the catalog-generation commands in [AGENTS.md](../AGENTS.md#add-a-model).
Keep requests on the governed application/gateway path when exercising models.

Removing a catalog entry does not delete its Azure deployment: ARM incremental
mode retains it. The postprovision topology gate rejects unexpected stale
deployments. Model retirement therefore requires an explicit, reviewed
live-resource cleanup as well as regenerated catalogs; it is not an automatic
side effect of editing JSON.
