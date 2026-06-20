# WS2 — Context management + per-model settings

**Goal:** make long conversations sustainable (rolling summarization with full
user-visible scrollback), give the agent an explicit recall tool over its own and
prior conversations, and make generation/context budgets scale with the chosen
model's window. These are combined because both rewrite the same hot path
(`routers/chat.py`) and the model's context window is the shared input that drives
summarization thresholds **and** the max-output cap.

## Current state (verified against `main`)

- **History:** the **entire** session history is sent to the model every turn —
  no windowing, no truncation (`routers/chat.py` `list_messages` + `_history()`;
  only `fromCommand` echoes filtered). UI shows full scrollback
  (`app/web/.../MessageList.tsx`). → Long chats will eventually overflow the model
  window.
- **Summarization:** **missing.** `/summarize` is a stub returning "coming soon"
  (`agents/command_service.py`); `CommandKind.summarize` defined but unimplemented.
- **Memory (mem0):** ✅ **user-scoped** semantic recall auto-injected every turn
  (`routers/chat.py` `memory.recall(user_id, …)`; `memory/service.py`), spans all
  of a user's sessions. Capped (~5 items / 500 chars each / 2000 total).
- **Agent recall tool:** **missing.** Builtin tools are only `calculator` +
  `get_current_time` (`agents/tool_exec.py`); no `search_conversation` /
  `recall` tool the agent can call.
- **Per-model:** max-tokens is user-adjustable (global, default 1024, 1–32000;
  `ParamControls.tsx`), but `ModelEntry` carries **no** `context_window` /
  `max_output_tokens` (`catalog.py`), there is **no per-model adaptation**
  (only reasoning models translate `max_tokens`→`max_completion_tokens` in
  `gateway/client.py`), and the doc-context budget is fixed 8K–12K
  (`config.py` `document_context_*` / `DOC_CONTEXT_BUDGET`).

## Target

### A. Per-model metadata (foundation — do first)

- Add `context_window: int | None` and `max_output_tokens: int | None` to
  `ModelEntry` (`catalog.py`), serialized so the web app reads from the same source
  of truth. Populate from `infra/models.json` (and the generator that produces
  `app/api/.../data/model_catalog.json`) for the conversational models.
- Expose the values in the model list API and the `ModelEntry` TS type.

### B. Per-model generation + context scaling

- In `routers/chat.py`, before calling the gateway, **cap** the user's
  `max_tokens` to the model's `max_output_tokens` (when known) and use the model's
  default when the user leaves it at the global default.
- Make the **doc/context budget** scale from `context_window` instead of the fixed
  constant (fall back to today's constant when metadata is absent).
- Frontend: `ParamControls` shows the active model's max-output cap and clamps the
  input; `ModelPicker` can show the context-window size.

### C. Rolling summarization (long-chat sustainability)

- Add a summarization service: when assembled history exceeds a threshold derived
  from the model's `context_window` (with headroom for system/memory/doc blocks +
  the max-output reservation), summarize the **oldest** turns into a compact
  running summary that is injected as a system block, while **keeping the full
  transcript in storage and in the UI** (scrollback unchanged). Newest N turns stay
  verbatim.
- Implement `/summarize` for real (replace the stub) so a user can force it; the
  automatic path uses the same service.
- Persist the rolling summary on the session (e.g. a `summary` field +
  `summarizedThroughMessageId`) so it is incremental, not recomputed from scratch.
- **Default-OFF flag** for the automatic path (manual `/summarize` can be always
  available); when off, behavior is exactly today's full-history send.

### D. Agent recall tool (cross-turn / cross-session)

- Add a governed builtin tool (e.g. `recall_memory` / `search_history`) the agent
  can call to semantically search the **user-scoped** memory store (reusing the
  existing mem0 recall, which already spans sessions) and/or the current
  conversation beyond the verbatim window. Closure-bind `user_id` (+ `session_id`
  for in-conversation scope); nonce-fence results like other synthetic tools;
  per-turn budget; fail-soft. Add it to the selectable/attachable tool sets.
- This makes the *already-existing* cross-conversation memory explicitly
  queryable by the agent, plus lets it reach past summarized turns.

## Files to change

- `app/api/src/ai4ia_api/catalog.py` — model metadata fields.
- `infra/models.json` + the catalog generator + `data/model_catalog.json` — populate
  context-window / max-output for conversational models.
- `app/api/src/ai4ia_api/routers/chat.py` — per-model token cap + context-budget
  scaling; wire summarization into history assembly; **this session owns this file**.
- `app/api/src/ai4ia_api/gateway/client.py` — ensure scaling composes with the
  existing reasoning-model param translation.
- New `app/api/src/ai4ia_api/agents/summarization.py` (or similar) — rolling-summary
  service.
- `app/api/src/ai4ia_api/agents/command_service.py` — implement `/summarize`.
- `app/api/src/ai4ia_api/agents/tool_exec.py` — register the recall tool + add to
  selectable/attachable sets.
- `app/api/src/ai4ia_api/sessions/models.py` — session summary fields.
- `app/api/src/ai4ia_api/config.py` — summarization flag + thresholds.
- `app/web/src/components/ModelPicker.tsx`, `ParamControls.tsx`, `lib/types.ts`,
  `ChatApp.tsx` — surface model limits + clamp max-tokens (coordinate with WS1 on
  `ChatApp.tsx`).

## Default-OFF / safety posture

- Automatic summarization behind a default-OFF flag; off ⇒ today's full-history
  send byte-for-byte.
- Token cap only ever **lowers** a too-high user value to the model's real limit;
  never raises silently beyond what the model accepts.
- Recall tool is closure-bound to the authenticated user and nonce-fenced; it
  cannot read another user's memory.

## Tests

- Model metadata round-trips catalog→API→TS; missing metadata falls back to current
  constants.
- `max_tokens` capped per model; reasoning translation still applied.
- Summarization: triggers past threshold; full transcript preserved in storage + UI;
  summary injected; incremental (only new tail summarized); off-flag = no-op.
- Recall tool: returns user-scoped results, nonce-fenced, respects per-turn budget,
  fail-soft on store error; cannot cross users.

## Acceptance criteria

- A long conversation that would exceed the window keeps working (summary + recent
  turns), and the user still scrolls the full original transcript.
- Choosing a large-context model raises the usable max-output cap accordingly; a
  small model clamps it.
- The agent can answer "what did we discuss earlier / in another chat?" via the
  recall tool.
- `ruff check .` clean; `pytest -q` green except the known Windows flakes;
  web `npm test` + build green.
