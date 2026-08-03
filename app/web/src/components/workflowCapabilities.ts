// Derives, for one workflow step, which capabilities that step will actually
// have when it runs — and, just as importantly, which it will not.
//
// This exists because a workflow step's tool surface is NOT its agent's tool
// list, and the difference is invisible at authoring time. Two rules in
// `agents/capabilities.py` are silent traps:
//
//   * The two memory tools are the only capabilities that must be deliberately
//     attached to the agent. Document reading and web search are ambient — every
//     step gets them. Nothing in the product said so, so a workflow written to
//     "remember the decisions" ran, replied that it could not save anything, and
//     was recorded as a success.
//   * An agent carrying `process_document`, `generate_image`, MCP tools, or
//     `delegate_to_agent` works in chat and *silently loses them* in a workflow,
//     because those deliver results through a per-turn attachment sink that a
//     durable activity finishes too far away from to drain.
//
// Everything here is derived from data the browser can already get. No new
// endpoint, and deliberately no guessing: where the server genuinely does not
// report something (see `web_search` below) the chip says so rather than
// inventing a verdict.

import { ATTACHABLE_TOOLS } from "@/lib/studio";
import type { ToolCatalogItem } from "@/lib/types";

export type ChipState =
  | "on"
  | "ambient"
  | "conditional"
  | "off"
  | "absent"
  | "chat-only";

export interface CapabilityChip {
  key: string;
  label: string;
  state: ChipState;
  /** Full explanation, rendered in a HelpTooltip beside the chip. */
  help: string;
  /** True when the remedy is "attach it to the agent" — drives the fix button. */
  fixable?: boolean;
}

// Mirrors the "Deliberately not shared" paragraph of agents/capabilities.py.
// A tool in this set is available in chat and structurally absent from a
// workflow step. Keep in step with that docstring.
export const NOT_IN_WORKFLOW_STEPS = new Set([
  "generate_image",
  "generate_video",
  "process_document",
  "analyze_attachment",
  "run_code",
  "export_document",
  "delegate_to_agent",
]);

// MCP tools are namespaced `server/tool`. They are excluded for a different
// reason than the set above: MCP *replaces* the registry/executor pair rather
// than adding to it, so a workflow step never sees one.
export function isMcpToolName(name: string): boolean {
  return name.includes("/");
}

// The tools a *step* may add to itself, DERIVED rather than hand-listed: every
// user-attachable tool, minus the ones that cannot work in a workflow step.
//
// Deriving it is the point. A hand-written list would happily offer
// `generate_image`, which would save cleanly, validate cleanly, and then do
// nothing — a checkbox for a capability that is structurally absent. Because
// this subtracts `NOT_IN_WORKFLOW_STEPS`, adding a tool to either list keeps the
// checkboxes honest with no second edit.
//
// The server deliberately accepts the *wider* agent-attachable set: it is one
// allowlist instead of two that can drift, and a step carrying a chat-only tool
// already renders an explicit "chat only" chip rather than a silent no-op.
export const STEP_ATTACHABLE_TOOLS: string[] = ATTACHABLE_TOOLS.filter(
  (t) => !NOT_IN_WORKFLOW_STEPS.has(t) && !isMcpToolName(t),
);

const MEMORY_TOOLS: Record<string, string> = {
  recall_memory: "Recall memory",
  remember_memory: "Save memory",
};

const REGISTRY_TOOLS: Record<string, string> = {
  calculator: "Calculator",
  get_current_time: "Current time",
};

export interface StepCapabilityInput {
  /** `inheritedTools` from GET /api/tools?agentName=… — the agent's own list. */
  attached: string[];
  /** This step's own `extraTools`, granted on top of the agent's list. */
  extra: string[];
  /** `tools` from the same response, for server-reported availability. */
  catalog: ToolCatalogItem[];
  /** How many library documents the run is scoped to; 0 means unscoped. */
  selectedDocCount: number;
  /** Friendly agent name, used in the copy that tells the user what to fix. */
  agentLabel: string;
}

function availability(catalog: ToolCatalogItem[], name: string): ToolCatalogItem | undefined {
  return catalog.find((t) => t.name === name);
}

export function stepCapabilities(input: StepCapabilityInput): CapabilityChip[] {
  const { attached, extra, catalog, selectedDocCount, agentLabel } = input;
  // What the step ACTUALLY runs with. `extraTools` is additive, so this union is
  // exactly the `effective_tools` the runner builds — the chips describe the run,
  // not the agent.
  const effective = [...attached, ...extra.filter((t) => !attached.includes(t))];
  const attachedSet = new Set(effective);
  const extraSet = new Set(extra);
  const chips: CapabilityChip[] = [];

  // `process_document`'s availability is computed from exactly the same object
  // that gates `fetch_document` server-side — the document-retrieval service —
  // so it is an exact proxy for "library retrieval exists", which is otherwise
  // not reported to the browser. Do not "clean this up" into a guess.
  const libraryService = availability(catalog, "process_document")?.available ?? false;
  const libraryDetail = availability(catalog, "process_document")?.detail;

  if (libraryService) {
    chips.push({
      key: "fetch_document",
      label:
        selectedDocCount > 0
          ? `Read documents · ${selectedDocCount} selected`
          : "Read documents · always on",
      state: "ambient",
      help:
        selectedDocCount > 0
          ? `Every step can read your library. This run is restricted to the ${selectedDocCount} document(s) you selected — the steps cannot see anything else.`
          : "Every step can read your document library. No attaching needed. With no documents selected for the run, a step can read any of your ready documents.",
    });
  } else {
    chips.push({
      key: "fetch_document",
      label: "Read documents · unavailable",
      state: "off",
      help: `This deployment has no document retrieval service, so steps cannot read your library.${
        libraryDetail ? ` ${libraryDetail}` : ""
      }`,
    });
  }

  // Web IQ is genuinely unreported. The five tools are synthetic extras, are not
  // in SELECTABLE_SYNTHETIC_TOOL_NAMES, and can never enter `effective_tools`
  // (which derives only from an agent's declared tools), so /api/tools does not
  // list them at all. Saying "on" here would be a fabrication.
  chips.push({
    key: "web_search",
    label: "Web search · if configured",
    state: "conditional",
    help: "Every step is offered web search, news, video, image search and page browsing whenever this deployment has Web IQ set up — no attaching needed. This page cannot confirm whether it is configured, because that is not reported to the browser.",
  });

  const memoryService =
    availability(catalog, "remember_memory")?.available ??
    availability(catalog, "recall_memory")?.available ??
    false;
  const memoryDetail =
    availability(catalog, "remember_memory")?.detail ??
    availability(catalog, "recall_memory")?.detail;

  for (const [name, label] of Object.entries(MEMORY_TOOLS)) {
    if (!attachedSet.has(name)) {
      chips.push({
        key: name,
        label: `${label} · off`,
        state: "absent",
        fixable: true,
        help:
          name === "remember_memory"
            ? `Neither this step nor its agent (${agentLabel}) has the Save memory tool, so this workflow cannot write anything to your memories. Worse, the model is not told that: it will reply as though it saved them, and the run will be recorded as successful. Switch it on under "Tools for this step". Unlike web search and document reading, the memory tools are only ever available when deliberately switched on.`
            : `Neither this step nor its agent (${agentLabel}) has the Recall memory tool, so it cannot look anything up in your saved memories. Switch it on under "Tools for this step".`,
      });
      continue;
    }
    if (!memoryService) {
      chips.push({
        key: name,
        label: `${label} · store disabled`,
        state: "off",
        help: `Switched on for this step, but this deployment has the memory store disabled, so nothing will be read or written.${
          memoryDetail ? ` ${memoryDetail}` : ""
        }`,
      });
      continue;
    }
    chips.push({
      key: name,
      label: extraSet.has(name) ? `${label} · this step` : label,
      state: "on",
      help:
        name === "remember_memory"
          ? "Available. This step can save short facts to your own memory, and reports honestly when a fact was already covered and nothing new was stored."
          : "Available. This step can search the memories you have saved.",
    });
  }

  for (const [name, label] of Object.entries(REGISTRY_TOOLS)) {
    if (!attachedSet.has(name)) continue;
    chips.push({
      key: name,
      label: extraSet.has(name) ? `${label} · this step` : label,
      state: "on",
      help: "Available in every execution mode.",
    });
  }

  for (const name of effective) {
    if (!NOT_IN_WORKFLOW_STEPS.has(name) && !isMcpToolName(name)) continue;
    chips.push({
      key: name,
      label: `${name} · chat only`,
      state: "chat-only",
      help: isMcpToolName(name)
        ? `${agentLabel} has this MCP tool, but MCP replaces the built-in tool set rather than adding to it, so a workflow step never gets it. It works when you chat with ${agentLabel} directly.`
        : `${agentLabel} has this tool, but it delivers its result as a chat attachment, which a workflow step has no way to deliver. It works when you chat with ${agentLabel} directly.`,
    });
  }

  return chips;
}
