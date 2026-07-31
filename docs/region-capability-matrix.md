# Region & capability map

**`infra/models.json` is the authoritative live catalog** — the exact models, versions,
regions, SKUs, and quotas that deploy. This doc is the *strategic* companion: which
regions we use and why, the Speech Voice Live catalog, and a retirement watch. When they
disagree, `models.json` wins; regenerate with `python scripts/gen-model-catalog.py`.

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
  evaluations. US data zone. Also the region for the `speech_voice_live` voice provider.
- **Primary EU — Sweden Central:** mirrors East US 2 for realtime/audio/`sora-2`/
  `model-router`/image, **adds `tts-hd`** and the `MAI-Image-2.5` family, and gives EU
  data residency.
- **Targeted — West US:** sole home of `o3-deep-research`, and the first region of the
  `MAI-Image-2.5` / `-Pro` / `-Flash` family. The MAI models are **not** offered in East
  US 2, so West US and Sweden Central are their only options; each carries both so a
  single-region outage does not take image generation down.

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

Every deployment still requires Azure quota/capacity confirmation in the target
subscription before `azd up`. Verify with `python scripts/check-model-availability.py`
(offering) plus `az cognitiveservices usage list --location <region>` (TPM quota) rather
than assuming: quota is granted **per model per region**, and entitlement differs between
subscriptions. As of the Planet Express standup, `gpt-5.5` and the `gpt-5.6` family all
carry default quota in both primary regions.

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
`validate-catalog.py` (see [`runbooks/deployment.md`](runbooks/deployment.md) §3
step 2). Confirm with `check-model-availability.py` **before** deploying —
approval is per-subscription and is not visible until the deployment step.
