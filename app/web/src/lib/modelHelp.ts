// Plain-language help copy for the conversational model *category* taxonomy
// shown in ModelPicker.tsx. Mirrors — and is grounded in — the backend's
// CONVERSATIONAL_CATEGORIES allowlist (api/catalog.py), which is the fixed,
// curated set of categories the chat/agent pickers ever show. This is not a
// per-model or per-deployment description (the catalog has none), so keying
// on category stays fully catalog-driven: adding a new deployment under an
// existing category needs no change here, and a genuinely new category
// simply shows no extra note until someone adds one.

export interface ModelCategoryHelp {
  /** Short, friendly name for the category (the raw value is a code, e.g. "chat-fast"). */
  label: string;
  /** What this kind of model is generally good at, in plain language. */
  what: string;
  /** When you'd want to pick a model from this category. */
  when: string;
  /** Tradeoffs — speed, cost, or predictability worth knowing before picking it. */
  tradeoffs: string;
}

export const MODEL_CATEGORY_HELP: Record<string, ModelCategoryHelp> = {
  chat: {
    label: "Chat",
    what: "A general-purpose conversational model.",
    when: "Use as your default for everyday questions, writing, and back-and-forth conversation.",
    tradeoffs: "Less capable than a reasoning model on multi-step logic, math, or planning-heavy asks.",
  },
  "chat-fast": {
    label: "Fast chat",
    what: "A lighter, quicker conversational model optimized for low latency.",
    when: "Use for simple questions or quick edits where a snappy reply matters more than depth.",
    tradeoffs: "Trades some quality and nuance for speed — switch to Chat or Reasoning for harder asks.",
  },
  reasoning: {
    label: "Reasoning",
    what: "A model that works through a problem step by step before answering.",
    when: "Use for multi-step logic, math, coding, or planning tasks that benefit from deeper thinking.",
    tradeoffs: "Usually slower and costs more per reply than a Chat model.",
  },
  "reasoning-oss": {
    label: "Open reasoning",
    what: "An open-weight model that also works through problems step by step.",
    when: "Use for the same kind of multi-step logic tasks as other reasoning models.",
    tradeoffs: "Usually slower than a Chat model; quality can vary more than vendor-tuned reasoning models.",
  },
  router: {
    label: "Auto-routed",
    what: "Automatically forwards each request to whichever underlying model fits it best.",
    when: "Use when you'd rather not choose manually and want a reasonable balance of quality and cost.",
    tradeoffs: "Which model actually answers can vary turn to turn, so behavior is less predictable.",
  },
  research: {
    label: "Deep research",
    what: "A model built for long, multi-step research that gathers and synthesizes information.",
    when: "Use for in-depth research questions where thoroughness matters more than a fast reply.",
    tradeoffs: "Typically the slowest and most expensive category — reach for it only for deep research asks.",
  },
};
