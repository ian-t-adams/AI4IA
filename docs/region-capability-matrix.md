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
