// Client-side mirrors of the backend's Studio validation caps and grammar
// (app/api .../agents/user_agents.py and .../workflows/models.py). These drive
// soft, pre-submit validation only — the backend is the source of truth and
// returns 422 with a `detail` message that the UI surfaces verbatim.

// Name must start lowercase, end alphanumeric/underscore; interior . _ - allowed.
export const STUDIO_NAME_RE = /^[a-z](?:[a-z0-9_.-]{0,30}[a-z0-9_])?$/;

export const MAX_NAME_LEN = 32;
export const MAX_DISPLAY_NAME_LEN = 80;
export const MAX_DESCRIPTION_LEN = 280;
export const MAX_SYSTEM_PROMPT_LEN = 8000;
export const MAX_TOOLS = 8;
export const MAX_LINKS = 5;

export const MAX_STEPS = 6;
export const MAX_INSTRUCTION_LEN = 4000;
// The backend also caps a step's `extraTools` (MAX_STEP_TOOLS), deliberately NOT
// mirrored here: the step picker only ever offers the handful of tools that work
// inside a workflow step, so a client-side cap could never fire. An unreachable
// guard reads as protection while providing none.
export const MAX_RUN_INPUT_LEN = 8000;
export const INPUT_TOKEN = "{input}";

// The backend's user-attachable tool allowlist. Mirrored here so the builder can
// offer the tools as checkboxes.
//
// Drift is NOT symmetric, which is why `test_attachable_tools_mirror.py` exists:
// a tool listed here but unknown to the API is rejected server-side with a 422,
// so it fails loudly. A tool the API allows but that is missing here has no
// checkbox, so it can never be attached to any agent — the capability is simply
// unreachable from the product with nothing to say so. That happened: the API
// gained `remember_memory` while this list did not, and agents correctly replied
// that they could not save memories.
export const ATTACHABLE_TOOLS = [
  "calculator",
  "get_current_time",
  "generate_image",
  "generate_video",
  "process_document",
  "recall_memory",
  "remember_memory",
] as const;

// Returns a human-readable reason the name is invalid, or null if it's valid.
export function nameError(name: string): string | null {
  if (!name) return "Name is required.";
  if (name.length > MAX_NAME_LEN) return `Name must be ≤ ${MAX_NAME_LEN} characters.`;
  if (!STUDIO_NAME_RE.test(name)) {
    return "Use lowercase letters, digits, . _ - ; start with a letter and end with a letter, digit, or underscore.";
  }
  return null;
}
