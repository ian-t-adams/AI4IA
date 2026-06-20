# WS1 — VoiceLive enhancements

**Goal:** let users choose the voice model, adjust voice-session settings, and use
tool calls in voice — without regressing the voice-selection and text↔voice
handoff that already work well.

## Current state (verified against `main`)

| Capability | Status | Evidence |
|---|---|---|
| Pick the TTS voice (10 voices) | ✅ works | `app/web/src/components/VoiceLivePanel.tsx` voice picker; `app/web/src/lib/voiceLive.ts` `REALTIME_VOICES`, `sessionUpdate()` |
| Smooth, repeatable text↔voice with shared context | ✅ works | `ChatApp.tsx` seeds recent history into voice (≤20 turns / 6000 chars); voice turns persist back via `appendVoiceTurns`; `voiceLive.ts` `seedFrames()` |
| Tool calls in voice | ⚠️ built, OFF | `routers/realtime.py` tool bridge + `inject_session_tools()`; gated on `settings.realtime_tools_enabled` (default `False`) |
| Pick the voice **model** | ❌ hardcoded | `ChatApp.tsx` auto-resolves first `realtime`-category model; `realtime.py` `resolve_realtime_deployment()` |
| Adjust **settings** (temperature, VAD, instructions, language, transcription) | ❌ hardcoded | `voiceLive.ts` `sessionUpdate()` — instructions/turn_detection/transcription fixed |

## Target

1. **Voice model picker** — surface the realtime-category models (there may be
   more than one, e.g. a GA realtime model + a mini) in the voice panel, mirroring
   the chat `ModelPicker`. Persist selection (localStorage, like the voice). The
   chosen model id flows through the existing `model` prop →
   `resolve_realtime_deployment(model_id=…)` which **already accepts an explicit
   model**. Validate server-side that the requested deployment is realtime-category
   (reject otherwise, same pattern as the chat non-conversational guard).
2. **Exposed session settings** — add an opt-in settings disclosure in
   `VoiceLivePanel` for: instructions (system prompt) override, temperature, VAD
   type (`server_vad` vs `semantic_vad` where supported) + threshold/silence,
   input-transcription model, and language hint. Thread these through
   `useVoiceLive` → `sessionUpdate()` payload. Keep **today's values as defaults**
   so existing behavior is byte-for-byte unchanged when the user touches nothing.
3. **Enable tool calls** — flip `realtime_tools_enabled` to be enableable in
   live (add/confirm an infra param + env wiring mirroring other feature flags),
   and add a UI affordance ("Allow tools in voice") that is only shown when the
   server advertises tools as available. Keep the **default OFF**. Tool execution
   already runs through the same governed bridge as chat — no new tool path.

## Files to change

- `app/web/src/components/VoiceLivePanel.tsx` — model picker, settings disclosure,
  tools toggle.
- `app/web/src/lib/voiceLive.ts` — accept model/settings/tools params; build them
  into `sessionUpdate()`; keep defaults identical to today.
- `app/web/src/lib/voiceLiveConfig.ts` — validate/resolve new options.
- `app/web/src/components/ChatApp.tsx` — pass selected realtime model + settings
  into the panel (coordinate with WS2, which also edits this file).
- `app/api/src/ai4ia_api/routers/realtime.py` — accept explicit realtime model and
  validate category; pass through session settings; keep tool bridge gated.
- `app/api/src/ai4ia_api/config.py` — any new voice-settings defaults; confirm
  `realtime_tools_enabled` is wired to infra.
- `infra/` — confirm/add a `realtimeToolsEnabled` param + env wiring
  (mirror `webSearchEnabled` / `inlineDocumentComputeEnabled`), **default false**.

## Default-OFF / safety posture

- Tool calls in voice remain **OFF by default**; flipping requires the infra flag.
- New settings default to the exact current hardcoded values → zero behavior change
  until a user opts in.
- Server validates the requested realtime model is realtime-category (no arbitrary
  deployment selection).

## Tests

- Frontend: model picker renders only realtime-category models; settings round-trip
  into the `sessionUpdate()` payload; defaults match today's fixed values when
  untouched; tools toggle hidden when server reports tools unavailable.
- Backend: `resolve_realtime_deployment` accepts a valid realtime model and rejects
  a non-realtime / unknown one; tool bridge still no-ops when
  `realtime_tools_enabled=false`; settings pass-through does not change the relay
  when defaults are sent.

## Acceptance criteria

- A user can pick among realtime models, adjust at least temperature + instructions
  + VAD, and (when enabled) get governed tool calls in a voice turn.
- With nothing toggled, a voice session is identical to today's.
- `ruff check .` clean; `pytest -q` (API) green except the two known
  Windows-only `test_library_repo` timestamp flakes; `npm test` + build green (web).
