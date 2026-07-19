// Plain-language help copy for the built-in tools any agent or workflow step
// can attach (see lib/studio.ts ATTACHABLE_TOOLS). The wording here mirrors —
// and is grounded in — the exact descriptions and risk levels the API
// registers server-side (agents/tool_exec.py's ToolSpec entries and
// routers/tools.py's _SYNTHETIC_DESCRIPTIONS + ToolRisk.safe assignment for
// generate_image/generate_video/process_document/recall_memory). Keeping one
// typed copy here means every checkbox/row that lets a user attach a
// built-in tool can show the same accurate what/when/tradeoffs explanation
// instead of a bare label or no help at all.

export type ToolRiskLevel = "safe" | "external" | "destructive";

export interface ToolHelpCopy {
  /** What the tool actually does, in plain language. */
  what: string;
  /** When you'd want an agent to have this tool. */
  when: string;
  /** Tradeoffs, cost, latency, or limits worth knowing before attaching it. */
  tradeoffs: string;
  /** Governance risk level; mirrors the backend ToolRisk enum exactly. */
  risk: ToolRiskLevel;
}

export const BUILT_IN_TOOL_HELP: Record<string, ToolHelpCopy> = {
  calculator: {
    what: "Evaluates a basic arithmetic expression (+ − × ÷ // %, parentheses, unary minus).",
    when: "Use for exact arithmetic instead of relying on the model to compute by reasoning.",
    tradeoffs: "No variables, functions, or exponentiation — complex expressions fail rather than guess.",
    risk: "safe",
  },
  get_current_time: {
    what: "Returns the current UTC date and time in ISO 8601 format.",
    when: "Use when the agent needs to know \u201cnow\u201d, e.g. to compute a deadline or timestamp something.",
    tradeoffs: "Always UTC — the agent (or you) must convert to a local time zone if needed.",
    risk: "safe",
  },
  generate_image: {
    what: "Generates an image and attaches the resulting file to the chat.",
    when: "Use when the user asks for a picture, illustration, diagram, or visual mockup.",
    tradeoffs: "Counts toward usage like any model call, and typically takes longer than a text reply.",
    risk: "safe",
  },
  generate_video: {
    what: "Generates a video and attaches the resulting file to the chat.",
    when: "Use when the user asks for a short video clip or animation.",
    tradeoffs: "Counts toward usage like any model call; usually the slowest built-in tool — reach for it only when a still image won't do.",
    risk: "safe",
  },
  process_document: {
    what: "Runs governed document tools (e.g. summarize, extract) against a document already uploaded to the Library.",
    when: "Use when the agent should work from a specific uploaded file instead of pasted text.",
    tradeoffs: "Only works once a document finishes processing and shows as Ready — it can't fetch arbitrary files.",
    risk: "safe",
  },
  recall_memory: {
    what: "Searches memories the signed-in user has previously saved and returns the relevant ones.",
    when: "Use when the agent should recall something saved earlier instead of relying only on the current conversation.",
    tradeoffs: "Only ever returns the current user's own memories, never another user's.",
    risk: "safe",
  },
};

// One-line, user-facing explanation of each governance risk level. Mirrors
// agents/tools.py's ToolRisk semantics — do not reword the meaning, only the
// phrasing, if this ever needs to change.
export function toolRiskSummary(risk: ToolRiskLevel): string {
  switch (risk) {
    case "safe":
      return "Safe: read-only or self-contained — no third-party network access.";
    case "external":
      return "External: this tool can reach third-party services outside this app.";
    case "destructive":
      return "Destructive: this tool can change or delete data.";
    default:
      return "Risk level not reported.";
  }
}
