// Slash-command definitions for the composer's "/" autocomplete menu. These
// mirror what the backend recognizes: built-in action commands handled in
// agents/command_service.py, plus the user-runnable tools (agents/tool_exec.py
// USER_ATTACHABLE_TOOL_NAMES + SELECTABLE_SYNTHETIC_TOOL_NAMES). The backend is
// the source of truth; an unknown "/x" simply replies "Unknown command", so this
// list only drives suggestions and can drift safe.

export interface SlashCommand {
  /** Token after the slash, e.g. "help" or "calculator". */
  name: string;
  /** Friendly label, e.g. "Help" or "Calculator". */
  label: string;
  /** One-line description shown under the label. */
  hint: string;
}

// Built-in action commands (no model involved; handled locally by the API).
const ACTION_COMMANDS: SlashCommand[] = [
  { name: "help", label: "Help", hint: "Show the available commands" },
  { name: "clear", label: "Clear", hint: "Clear this conversation's history" },
  { name: "system", label: "System prompt", hint: "Set the system prompt for this chat" },
  { name: "model", label: "Model", hint: "Switch the model for this chat" },
  { name: "agents", label: "Agents", hint: "List the agents you can mention" },
  { name: "summarize", label: "Summarize", hint: "Condense the conversation into a running summary" },
  { name: "forget", label: "Forget", hint: "Erase stored memories (session | me)" },
];

// Tools runnable directly as a slash command. Calculator/current-time run
// instantly; research / generate_* / process_document / recall_memory drive the
// matching capability through a normal turn.
const TOOL_COMMANDS: SlashCommand[] = [
  { name: "calculator", label: "Calculator", hint: "Evaluate an arithmetic expression" },
  { name: "get_current_time", label: "Current time", hint: "Show the current UTC time" },
  { name: "research", label: "Research", hint: "Search the live web and cite sources" },
  { name: "generate_image", label: "Generate image", hint: "Create an image from a description" },
  { name: "generate_video", label: "Generate video", hint: "Create a short video from a description" },
  { name: "process_document", label: "Process document", hint: "Analyze a document in your library" },
  { name: "run_workflow", label: "Run workflow", hint: "Run one of your saved safe workflows" },
  { name: "recall_memory", label: "Recall memory", hint: "Search your saved memories" },
];

export const SLASH_COMMANDS: SlashCommand[] = [...ACTION_COMMANDS, ...TOOL_COMMANDS];
