# AI4IA — Region × Capability Map (evidence-based)

Source: `az cognitiveservices model list` per region (deployable models + SKUs) on the
`slurmfactory` subscription, plus Microsoft Learn region-availability docs for platform
services. Legend: **G**=GlobalStandard, **Z**=DataZoneStandard, **S**=Standard, **-**=not available.

## Data zone semantics
- US regions (eastus, eastus2, westus, westus3): DataZoneStandard `Z` = **United States** data zone.
- swedencentral: DataZoneStandard `Z` = **European Union** data zone (data residency).
- Newest chat/grok models in swedencentral are frequently **GlobalStandard-only** (no `Z`).

## Platform service availability (among our 5 candidates)
| Capability | eastus | eastus2 | westus | westus3 | swedencentral |
|---|---|---|---|---|---|
| Chat/reasoning models | ✅ (99) | ✅ (113) | ✅ (98) | ✅ (97) | ✅ (112) |
| Voice/realtime/audio **models** (gpt-realtime, gpt-audio) | – | ✅ | – | – | ✅ |
| tts / tts-hd | – | – | – | ✅ | ✅ |
| gpt-4o-mini-tts | – | ✅ only | – | – | – |
| model-router | – | ✅ | – | – | ✅ |
| gpt-image-1/1.5/2 | – | ✅ | – | ✅ | ✅ |
| MAI-Image-2 / 2.5 / 2e | ✅ | – | ✅ | – | ✅ |
| sora / sora-2 (video) | – | ✅ | – | – | ✅ |
| o3-deep-research | – | – | ✅ only | – | – |
| Content Understanding | ✅ | ✅ | ✅ | ✅ | ✅ |
| Agent Service (Standard) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Voice Live API | ✅ | ✅ | ✅ | ✅ | ✅ |
| Evaluations (confirmed) | – | ✅ | – | – | ✅ |

> Voice Live API is broadly listed, but it consumes realtime/audio models which only deploy
> in eastus2 + swedencentral — so those are the practical Voice Live regions.

## Recommended region strategy
- **Primary US — East US 2:** richest overall; the only region with `gpt-4o-mini-tts`, plus
  full voice/realtime/audio, image, sora, model-router, evaluations. US data zone.
- **Primary EU — Sweden Central:** mirrors eastus2 for voice/realtime/audio/sora/model-router/
  image, **adds tts/tts-hd**, EU data residency, evaluations confirmed.
- **Add West US (targeted):** the **MAI-Image-2.x** family and **o3-deep-research** live here
  and nowhere in the primary pair.
- **eastus / westus3 — optional capacity overflow:** westus3 (gpt-image + tts/tts-hd, no
  realtime/audio); eastus (dall-e-3 + MAI-Image). Not v1 core; enable later for quota spread.

## Forward-looking model SKU matrix (curated)
Columns: eastus | eastus2 | westus | westus3 | swedencentral

```
model                              eus    eus2   wus    wus3   swc
Cohere-rerank-v4.0-pro             G      G      G      G      G
DeepSeek-V3.2 / V3.2-Speciale      G      G      G      G      G
embed-v-4-0                        G      G      G      G      G
FLUX.2-pro                         GZ     GZ     GZ     GZ     GZ
gpt-4.1 / 4.1-mini                 GZS    GZS    GZS    GZS    GZS
gpt-5 / 5-mini / 5-nano            GZ     GZ     GZ     GZ     GZ
gpt-5.1                            GZS    GZS    GZS    GZS    GZS
gpt-5.2                            GZ     GZ     GZ     GZ     G
gpt-5.3-codex                      GZ     GZ     GZ     GZ     G
gpt-5.4 / 5.5                      GZ     GZ     GZ     GZ     GZ
gpt-5.4-pro                        -      G      -      -      G
gpt-audio / gpt-audio-1.5/mini     -      G      -      -      G
gpt-image-1 / 1-mini / 2           -      G      -      G      G
gpt-image-1.5                      -      GZ     -      GZ     GZ
gpt-oss-120b                       G      GZ     G      G      G
gpt-realtime / 1.5 / 2 / mini      -      G      -      -      G
grok-4-fast-reasoning / grok-4.3   GZ     GZ     GZ     GZ     G
MAI-Image-2 / 2.5 / 2.5-Flash/2e   G      -      G      -      G
MAI-DS-R1                          G      G      G      G      G
model-router                       -      GZ     -      -      GZ
o3 / o3-mini / o4-mini             GZ(S)  GZ(S)  GZ(S)  GZ(S)  GZ(S)
o3-deep-research                   -      -      G      -      -
o3-pro                             -      G      -      -      G
sora / sora-2                      -      G(S)   -      -      G
text-embedding-3-large             GZS    GZS    GZ     GZS    GZS
tts / tts-hd                       -      -      -      S      S
whisper                            +      S      +      +      S
```

## Model lifecycle / refresh (2026-07)

Audit date: **2026-07-08**. `infra/models.json` remains the deploy source of truth.
The adds and the `gpt-4.1-nano` removal described below are **now applied** to the
catalog (regenerated + validated), but every deployment still requires quota/capacity
confirmation in the target Azure subscription before `azd up`.

### At-risk catalog entries

| Model | Category | Current version | Retirement / risk | Recommendation |
|---|---|---:|---|---|
| `whisper` | transcription | `001` | retirement floor 2026-02-01 has passed | Plan migration off legacy Foundry speech; prefer GPT audio/realtime where it fits the app flow, or Azure AI Speech for dedicated STT. |
| `gpt-4o-mini-tts` | tts | `2025-03-20` | retirement floor 2025-09-17 has passed | Treat as highest-risk; evaluate GPT audio/realtime speech output or Azure AI Speech TTS before removal. |
| `tts-hd` | tts | `001` | retirement floor 2026-02-01 has passed | Same as above; keep until replacement is validated in deployed voice paths. |
| `gpt-4.1-mini` | chat-fast | `2025-04-14` | deprecated; retires 2026-10-14 | **Kept — load-bearing** (memory-extraction model + code-interpreter default). Migrate both refs to `gpt-5.4-mini` before removal. |
| `gpt-4.1-nano` | chat-fast | `2025-04-14` | deprecated; retires 2026-10-14 | **Removed 2026-07-08**; superseded by `gpt-5.4-nano`. |
| `gpt-5.2` | chat | `2025-12-11` | retires 2026-12-12 | Add/validate `gpt-5.5` and begin default-model evaluation. |
| `gpt-image-1.5` | image | `2025-12-16` | retires 2026-12-16 | Keep for compatibility; prefer `gpt-image-2`, add `gpt-image-1-mini` for cheaper image jobs. |
| `gpt-realtime-mini` | realtime | `2025-12-15` | retires 2026-12-15 | Add `gpt-realtime-2` / `gpt-realtime-1.5` before changing Voice Live defaults. |

### Adds / swaps — deployment-type & quota investigation (2026-07-08)

PR #149 added 8 models; the `main` deploy then failed with `InsufficientQuota` on
`gpt-5.5`. A live check against the deploy subscription (`az cognitiveservices model list`
+ `az cognitiveservices usage list`, eastus2 / swedencentral) settled the deployment-type
and region questions:

- **Deployment type:** `GlobalStandard` is correct for all of these models in eastus2 +
  swedencentral. Four (`gpt-5.4-pro`, `gpt-audio-1.5`, `gpt-realtime-2`, `gpt-image-1-mini`)
  offer *only* GlobalStandard there; the rest also offer DataZoneStandard, but Global has
  equal-or-higher quota. No DataZone switch is needed.
- **Regions:** no new regions needed — every model is available in both primary regions,
  and several are *not* offered in westus / eastus / northcentralus, so eastus2 +
  swedencentral are the right homes.
- **Root cause was quota, and only for one model.** `gpt-5.5` has a hard quota of **0**
  (GlobalStandard and DataZoneStandard, both regions). The deploy aborted on it before
  reaching the others, which made #150 over-remove; the other 6 have quota and are re-added.

| Model | GlobalStandard quota (K TPM, eastus2 / swedencentral) | Status |
|---|---|---|
| `gpt-audio-1.5` | 30000 / 30000 | **re-added** |
| `gpt-5.4-nano` | 5000 / 5000 | **re-added** |
| `gpt-5.3-codex` | 1000 / 1000 | **re-added** |
| `gpt-5.4-pro` | 160 / 160 | **re-added** |
| `gpt-realtime-2` | 10 / 10 | **re-added** |
| `gpt-image-1-mini` | 4 / 4 | **re-added** |
| `gpt-5.5` | 0 / 0 | **held out — needs a quota request** |
| `MAI-Image-2.5-Flash` (westus) | n/a (MAI image quota) | kept (deployed in #149) |

To bring in `gpt-5.5`: Portal -> Quotas -> Cognitive Services / Azure AI Foundry -> request
"Tokens Per Minute - gpt-5.5 - GlobalStandard" in eastus2 or swedencentral, then restore its
entry in `infra/models.json`, run `python scripts/gen-model-catalog.py`, and redeploy.

### Removals applied 2026-07-08

- `gpt-4.1-nano` — removed (no code references); its successor `gpt-5.4-nano` is deployed (quota confirmed).
- `MAI-Image-2.5` — kept in **westus** only. The Sweden Central (EU) deployment added in #149
  was reverted because its quota was not confirmed in the failed deploy; re-add once MAI image
  quota in Sweden Central is validated.

### Retained (load-bearing) — migrate in a dedicated change

- `gpt-4.1-mini` is **kept**: it is the memory-extraction model (`config.py`
  `memory_extraction_model`) and the default code-interpreter model (`main.bicep`
  `effectiveCodeInterpreterModel`). Migrate both refs to a current model (e.g.
  `gpt-5.4-mini`, after confirming Responses-API code-interpreter support) before removing.
- `whisper` / `tts-hd` / `gpt-4o-mini-tts` are the current Azure OpenAI STT/TTS models
  backing the voice REST endpoints (still documented + supported); keep until a validated
  Foundry / Azure AI Speech replacement is wired into the voice paths.
- `gpt-5.2` remains the default chat model across the app and tests; bump the default to
  `gpt-5.4` / `gpt-5.5` in a dedicated change before its 2026-12-12 retirement.
The deployment still needs Azure quota/capacity validation before the next `azd up`.
