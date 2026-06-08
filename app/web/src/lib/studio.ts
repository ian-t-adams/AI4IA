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
export const MAX_RUN_INPUT_LEN = 8000;
export const INPUT_TOKEN = "{input}";
export const PREVIOUS_TOKEN = "{previous}";

// The backend's user-attachable tool allowlist. Mirrored here so the builder can
// offer the tools as checkboxes; unknown tools are rejected server-side (422), so
// drift fails safe rather than silently.
export const ATTACHABLE_TOOLS = ["calculator", "get_current_time"] as const;

// Returns a human-readable reason the name is invalid, or null if it's valid.
export function nameError(name: string): string | null {
  if (!name) return "Name is required.";
  if (name.length > MAX_NAME_LEN) return `Name must be ≤ ${MAX_NAME_LEN} characters.`;
  if (!STUDIO_NAME_RE.test(name)) {
    return "Use lowercase letters, digits, . _ - ; start with a letter and end with a letter, digit, or underscore.";
  }
  return null;
}
