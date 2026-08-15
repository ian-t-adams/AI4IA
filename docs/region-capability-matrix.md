# Region & capability map

**`infra/models.json` is the authoritative live catalog** — the exact models, versions,
regions, SKUs, quotas, API surface, and accepted `reasoning_effort` values that deploy.
This doc is the *strategic* companion: which regions we use and why, the Speech Voice
Live catalog, and a retirement watch. When they disagree, `models.json` wins; regenerate
with `python scripts/gen-model-catalog.py`.

Deploy regions today: **East US 2**, **Sweden Central**, **West US** (see
`infra/models.json` `regions`).

## Data-zone semantics

- US regions (eastus2, westus): `DataZoneStandard` = **United States** data zone.
- swedencentral: `DataZoneStandard` = **European Union** data zone (EU data residency).
- The newest chat models in swedencentral are frequently **GlobalStandard-only** (no data-zone SKU).

## Region strategy

- **Primary US — East US 2:** richest region. Only home of `gpt-4o-mini-tts`; hosts the
  realtime/audio models (`gpt-realtime`, `gpt-realtime-2` (eastus2-only, 10 RPM quota),
  `gpt-audio`), images (`gpt-image-1-mini/1.5/2`), `sora-2`, `model-router`, and
  evaluations. It hosts `MAI-Thinking-1` here at 50K TPM under
  `GlobalStandard`; that SKU is globally routed and does **not** provide a US data
  boundary. It also hosts the four Black Forest Labs FLUX image models at one
  Global Standard unit each. Also the region for the `speech_voice_live` voice provider.
- **Primary EU — Sweden Central:** mirrors East US 2 for realtime/audio/`sora-2`/
  `model-router`/image and **adds `tts-hd`**. It hosts
  `MAI-Thinking-1` here at 50K TPM under `GlobalStandard`, so that model is not
  EU-resident despite the region. It mirrors the four FLUX image deployments at
  one Global Standard unit each. It *offers* the `MAI-Image-2.5` family but does
  not deploy it — see West US below.
- **Targeted — West US:** sole home of `o3-deep-research` and of the `MAI-Image-2.5` /
  `-Pro` / `-Flash` family. That **image family** is pinned to West US alone, which is
  a quota constraint rather than a design preference:

  > MAI-Image quota is **subscription-wide**, not per-region, and the default limit is 2
  > units per model. Deploying capacity 2 in West US consumes the whole allowance, so a
  > second region deterministically fails with `InsufficientQuota` — whichever region ARM
  > reaches first wins. This cost a provision run before it was understood; see
  > `docs/runbooks/deployment.md` §7.9. `scripts/check-model-availability.py` now blocks on
  > it, and `scripts/tests/test_subscription_preflight.py::SharedQuotaTests` pins MAI-Image
  > to a single region so the second one cannot be re-added by accident.
  >
  > **To restore the second region:** request a MAI-Image quota increase to at least 4 per
  > model in this subscription, then add a `swedencentral` deployment block (capacity 2,
  > same versions) back to each of the three models in `infra/models.json`, relax the guard
  > test, and re-run `gen-model-catalog.py` / `gen-gateway-policy.py` / `validate-catalog.py`.

  Note this leaves image generation without regional redundancy, unlike the OpenAI image
  models (`gpt-image-*`), which are enforced per region and so do carry two regions each.

### MAI capacity and version review (2026-08-12)

> **Point-in-time snapshot of one subscription.** Quota, platform capacity, and
> model availability change continuously. Run `python scripts/check-model-availability.py`
> and `az cognitiveservices usage list --location <region>` against your own
> subscription before acting on this table.

| Model | Catalog state | Subscription/platform capacity | Decision |
|---|---|---|---|
| `MAI-Thinking-1` `2026-06-01` | Public Preview, current/default; both deployments `Succeeded` | 100K of 500K quota used | Live at 50K in East US 2 + 50K in Sweden Central, preserving 400K headroom and regional failover. Both use globally routed `GlobalStandard`. |
| `MAI-Image-2.5` `2026-06-02` | Current Preview | 2/2 subscription quota consumed; platform reports 0 additional capacity | No upgrade or scale change. |
| `MAI-Image-2.5-Flash` `2026-06-02` | Current Preview | 2/2 consumed; platform reports 0 additional capacity | No upgrade or scale change. |
| `MAI-Image-2.5-Pro` `2026-06-19` | Current Preview | 2/2 consumed; platform reports 0 additional capacity | No upgrade or scale change. |
| `MAI-Image-2` / `MAI-Image-2e` | Older and deprecating; not deployed | Unused legacy counters | Do not add; 2.5 family is the successor already deployed. |
| `MAI-DS-R1` | Quota counter exists, but no deployable offer in the checked regions | 1000K unused quota is not entitlement | Do not add unless the live model catalog offers it again. |

### FLUX and requested-model review (2026-08-12)

> **Point-in-time snapshot of one subscription.** Quota, platform capacity, and
> model availability change continuously. Run `python scripts/check-model-availability.py`
> and `az cognitiveservices usage list --location <region>` against your own
> subscription before acting on this table.

| Model/family | Live subscription state | Decision |
|---|---|---|
| `FLUX.2-pro`, `FLUX.2-flex` | GA; both regional deployments `Succeeded`; each region reports 1/4 Global Standard units used | Live at capacity 1 in each primary region. Route the BFL-native API through SimpleL7Proxy -> APIM -> Foundry; keep deployment selection and `safety_tolerance=2` server-owned. |
| `FLUX.1-Kontext-pro`, `FLUX-1.1-pro` | GA; both regional deployments `Succeeded`; each region reports 1/30 Global Standard units used | Live at capacity 1 in each primary region. Kontext is constrained to square 1024 output; all FLUX models accept only `auto` in the app's generic quality control. |
| `DeepSeek-V4-Pro` / `DeepSeek-V4-Flash` `2026-04-23` | **Not deployed.** Both are offered; live quota/platform capacity is 1000/1000 for Pro and 250/250 for Flash. | Do not silently add to agents: Microsoft documents 1M input, 384K output, and **no tool calling**. They are viable future chat-only models after the app gains a server-authoritative tool-capability gate. |
| `qwen3-32b` v1 | **Not deployed.** The offer is visible, but platform inference capacity is 0 in all three checked regions; the 1000-unit counter is named `Qwen3-32B-finetune`. | No deploy: a fine-tuning counter is not inference entitlement. |
| `Kimi-K2.6` `2026-04-20` | **Not deployed.** Preview; 100 units of live quota/platform capacity. | Viable future chat/agent model (262K text+image input/output, tool calling), but outside this FLUX deployment change. |
| `Mistral-Large-3` v1 | **Already deployed:** Global + Data Zone capacity 50 in both primary regions. Live control plane reports GA. | Keep; it is already available for chat and agents. |
| `mistral-document-ai-2512` / `mistral-ocr-4-0` | GA/Preview respectively; final-name Global Standard deployments are verified `Succeeded` at capacity 1 in both East US 2 and Sweden Central. | Dedicated Analyzer pathways, not chat models. Explicit selection, canonical Markdown normalization, deployment/page provenance, and page-based metering are live; Content Understanding remains the automatic default. |

> Voice Live is broadly available as an API, but it consumes realtime/audio models that
> only deploy in East US 2 + Sweden Central — so those are the practical Voice Live regions.

## Speech Voice Live managed-model catalog

The `speech_voice_live` provider is pinned to the East US 2 AIServices account and API
version `2026-04-10`. The server accepts exactly this catalog (see
`infra/voice-providers.json`); Azure OpenAI Realtime remains the app default. Adding
Sweden Central requires a separate catalog, APIM API, and RBAC change.

| Managed model | Response path | Input transcription |
|---|---|---|
| `gpt-realtime` (default) | native audio | `gpt-4o-transcribe` |
| `gpt-realtime-mini` | native audio | `gpt-4o-transcribe` |
| `gpt-4.1` | Azure Speech chain | `azure-speech` |
| `gpt-4.1-mini` | Azure Speech chain | `azure-speech` |
| `gpt-5-mini` | Azure Speech chain | `azure-speech` |
| `gpt-5.1` | Azure Speech chain | `azure-speech` |

## Retirement & load-bearing watch

Forward-looking only — confirm current versions and retirement dates in `models.json` and
the Azure AI Foundry portal before acting.

| Model | Category | Risk | Action |
|---|---|---|---|
| `whisper` | transcription | retirement floor passed | Plan migration off legacy Foundry speech (GPT audio/realtime, or Azure AI Speech STT). |
| `gpt-4o-mini-tts` | tts | retirement floor passed | Highest risk; evaluate GPT audio/realtime output or Azure AI Speech TTS before removal. |
| `tts-hd` | tts | retirement floor passed | Keep until a replacement is validated in the deployed voice paths. |
| `gpt-4.1-mini` | chat-fast | **REMOVED 2026-07-31** | Azure moved it to `Deprecating`, which blocks *new* deployments (`ServiceModelDeprecating`) while existing ones keep serving — so a clean provision failed even though the model was still listed and quotaed. No non-deprecating version exists (the whole GPT-4.1 family is deprecating). Both load-bearing refs were migrated to `gpt-5.4-mini`: `config.py memory_extraction_model` and `main.bicep effectiveCodeInterpreterModel`. |
| `o4-mini` | reasoning | **REMOVED 2026-07-31** | Same `Deprecating` state, no successor version. `reasoning` still has 6 models. |
| `gpt-5.2` | chat | retires 2026-12-12 | Default chat model across app + tests; bump the default before retirement in a dedicated change. |
| `gpt-image-1.5` | image | retires 2026-12-16 | Keep for compatibility; prefer `gpt-image-2`. |
| `gpt-realtime-mini` | realtime | retires 2026-12-15 | Validate `gpt-realtime-2` before changing Voice Live defaults. |
| `MAI-Thinking-1` | reasoning | Preview version retires 2026-11-04 | Evaluate now; watch for a replacement/default version before November and re-probe request parameters on upgrade. |

Every deployment still requires Azure quota/capacity confirmation in the target
subscription before `azd up`. Verify with `python scripts/check-model-availability.py`
(offering) plus `az cognitiveservices usage list --location <region>` (TPM quota) rather
than assuming: quota is granted **per model per region**, and entitlement differs between
subscriptions. As of the Planet Express standup, `gpt-5.5` and the `gpt-5.6` family all
carry default quota in both primary regions. Reasoning-effort support is probed and
recorded per model in the catalog rather than inferred from provider docs; do not add a
new value without re-probing the live deployment.

### `o3-pro` removed from the catalog

`o3-pro` was dropped from `infra/models.json` because it is **not offered in the
Planet Express subscription in either primary region** — it was the only entry
that failed `check-model-availability.py`, and an unavailable deployment is a
hard `azd provision` failure, not a warning. Keeping it would have blocked the
tenant cutover on a model nothing depends on.

No capability was lost: `o3-pro` is the previous generation of the pro-tier
reasoning slot, and `gpt-5-pro` and `gpt-5.4-pro` both occupy it, both in the
`reasoning` category, both available in `eastus2` and `swedencentral`. Plain
`o3` also remains.

To restore it after an access request is approved, re-add the entry to
`infra/models.json`:

```jsonc
{
  "name": "o3-pro",
  "format": "OpenAI",
  "category": "reasoning",
  "api": "responses",
  "contextWindow": 200000,
  "maxOutputTokens": 100000,
  "deployments": [
    { "region": "eastus2",       "sku": "GlobalStandard", "capacity": 50, "version": "2025-06-10" },
    { "region": "swedencentral", "sku": "GlobalStandard", "capacity": 50, "version": "2025-06-10" }
  ]
}
```

then re-run `gen-model-catalog.py`, `gen-gateway-policy.py`, and
`validate-catalog.py` (see
[greenfield standup §1](runbooks/greenfield-standup.md#1-preflight-the-target)).
Confirm with `check-model-availability.py` **before** deploying —
approval is per-subscription and is not visible until the deployment step.

### `Cohere-rerank-v4.0-pro` removed from the catalog

`Cohere-rerank-v4.0-pro` was deployed in both primary regions and carried into the
model catalog and the generated APIM backend catalog, but **nothing could call
it**:

- No application code referenced it. Retrieval reranking is the Azure AI Search
  L2 semantic reranker inside `AzureSearchDocChunkStore`, not a Cohere call.
- It had no `pricing.json` entry, so any use would have been cost-unknown.
- The generated route used `path: "openai"` — the generator defaults every
  unrecognised `api` to the OpenAI surface — while Microsoft documents rerank as
  reachable **only** through Cohere's own rerank API, not the OpenAI surface.
- Probing the live deployment confirmed it: `/cohere/v2/rerank`, `/cohere/v1/rerank`,
  `/v2/rerank`, `/v1/rerank`, `/models/rerank`, `/cohere/rerank` and
  `/openai/deployments/<name>/rerank` all returned **404** against the AI Services
  account endpoint, with and without an `api-version`.

So it was a `rerank`-category entry with no served surface — deployed capacity the
app had no way to reach. `scripts/gen-gateway-policy.py` now fails generation for
any category outside `ROUTABLE_CATEGORIES` instead of fabricating an OpenAI route,
which is what let this ship silently.

No capability was lost: hybrid vector + BM25 retrieval with L2 semantic reranking
is unchanged, and `embed-v-4-0` (also Cohere) remains deployed and is selectable
via `AI4IA_MEMORY_EMBEDDING_MODEL`.

To restore it once a supported rerank surface exists, re-add the entry to
`infra/models.json`:

```jsonc
{
  "name": "Cohere-rerank-v4.0-pro",
  "format": "Cohere",
  "category": "rerank",
  "deployments": [
    { "region": "eastus2",       "sku": "GlobalStandard", "capacity": 50, "version": "1", "maxCapacity": 150, "maxCapacityPool": "global" },
    { "region": "swedencentral", "sku": "GlobalStandard", "capacity": 50, "version": "1", "maxCapacity": 150, "maxCapacityPool": "global" }
  ]
}
```

then give `rerank` a real provider path in `gen-gateway-policy.py`, add it to
`ROUTABLE_CATEGORIES`, add a `pricing.json` rate, and wire an actual consumer —
otherwise the generator will (correctly) refuse to build the catalog.

> **Removing a model from `models.json` is only half the change.** Bicep model
> deployments are incremental: dropping an entry stops *declaring* the deployment
> but does not delete the live one, and `postprovision.ps1` then hard-fails the
> next deploy with `unexpected stale deployment(s)`. That gate is deliberate — it
> refuses to let the catalog and the subscription drift apart — but it means the
> live deployments must be deleted in the same change:
>
> ```powershell
> az cognitiveservices account deployment delete -g <rg> -n <account> `
>   --deployment-name <model>-<token>-<region>-<skuShort>
> ```
>
> Removing `Cohere-rerank-v4.0-pro` hit exactly this: the deploy provisioned
> cleanly, then failed post-provision on both regions and rolled the app
> revisions back until the two orphaned deployments were deleted.
