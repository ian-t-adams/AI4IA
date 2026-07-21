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
  `model-router`/image, **adds `tts-hd`**, and gives EU data residency.
- **Targeted — West US:** home of the `MAI-Image-2.5` / `MAI-Image-2.5-Flash` family and
  `o3-deep-research`, which live nowhere in the primary pair.

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
| `gpt-4.1-mini` | chat-fast | deprecated; retires 2026-10-14 | **Load-bearing** — the memory-extraction model (`config.py memory_extraction_model`) and default code-interpreter model (`main.bicep effectiveCodeInterpreterModel`). Migrate both refs (e.g. to `gpt-5.4-mini`) before removal. |
| `gpt-5.2` | chat | retires 2026-12-12 | Default chat model across app + tests; bump the default before retirement in a dedicated change. |
| `gpt-image-1.5` | image | retires 2026-12-16 | Keep for compatibility; prefer `gpt-image-2`. |
| `gpt-realtime-mini` | realtime | retires 2026-12-15 | Validate `gpt-realtime-2` before changing Voice Live defaults. |

Every deployment still requires Azure quota/capacity confirmation in the target
subscription before `azd up` (e.g. `gpt-5.5` needs a TPM quota request before it can be
added to `models.json`).
