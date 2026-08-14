/**
 * Starter workflow templates.
 *
 * A workflow is a saved, ordered pipeline of agent steps. Authoring a good one
 * from an empty form requires knowing which tools a *step* can actually use,
 * which is not obvious and not the same as what chat can use:
 *
 * * `fetch_document` is **automatic** in every step whenever the document
 *   library is on. It is not user-attachable and must not appear in
 *   `extraTools` -- the server builds it from the retrieval service, not from
 *   the step's tool list.
 * * `process_document`, `generate_image`, `generate_video` and `run_workflow`
 *   are **chat-only**. They deliver results through a per-turn attachment sink
 *   that a workflow run has no way to drain, so a step that names one gets
 *   nothing. Historically that failed silently: the model narrated work it had
 *   not done and the run was persisted as a success.
 *
 * So a document workflow reads already-ingested library documents; it cannot
 * perform the ingestion itself. Ingestion happens when the user uploads a file
 * and picks an analyzer (a Content Understanding analyzer, or Mistral). These
 * templates therefore start *after* that upload and turn a parsed document into
 * something useful, which is the part worth saving and re-running.
 *
 * `WORKFLOW_STEP_TOOLS` mirrors the server's own split. It is enforced by
 * `workflowTemplates.test.ts` rather than trusted, because a template naming a
 * chat-only tool would reintroduce exactly the silent failure above.
 */
import type { WorkflowCreate, WorkflowStep } from "./types";

/** Tools a workflow *step* can genuinely use (chat-only ones excluded). */
export const WORKFLOW_STEP_TOOLS = [
  "calculator",
  "get_current_time",
  "recall_memory",
  "remember_memory",
] as const;

/**
 * Tools the API exposes to a user but that a workflow step cannot run.
 * Mirrors `CHAT_ONLY_SYNTHETIC_TOOL_NAMES` in the API's `tool_exec.py`.
 */
export const CHAT_ONLY_TOOLS = [
  "generate_image",
  "generate_video",
  "process_document",
  "run_workflow",
] as const;

/**
 * A template step. `extraTools` is optional on {@link WorkflowStep} only so
 * workflows saved before that field existed still parse on read; a template is
 * authored fresh, so requiring it here forces every step to state its tools
 * explicitly instead of inheriting an implicit empty list.
 */
type TemplateStep = WorkflowStep & { extraTools: string[] };

export interface WorkflowTemplate {
  /** Stable id for the picker; not sent to the server. */
  readonly id: string;
  /** One-line explanation of when to reach for this template. */
  readonly blurb: string;
  /**
   * `Omit` rather than an intersection: `WorkflowCreate & { steps: TemplateStep[] }`
   * leaves `steps` as `WorkflowStep[] & TemplateStep[]`, and TS then widens the
   * mapped element back to `WorkflowStep`, making `extraTools` optional again at
   * every call site — defeating the point of requiring it.
   */
  readonly workflow: Omit<WorkflowCreate, "steps"> & { steps: TemplateStep[] };
}

export const WORKFLOW_TEMPLATES: readonly WorkflowTemplate[] = [
  {
    id: "cu-document-review",
    blurb:
      "Content Understanding: turn a parsed document's structured fields and confidence evidence into a reviewed summary.",
    workflow: {
      name: "cu-doc-review",
      displayName: "Document review (Content Understanding)",
      description:
        "Reads a document already analyzed by a Content Understanding analyzer, checks its extracted fields against the source text, and writes a reviewed summary.",
      enabled: true,
      steps: [
        {
          agent: "researcher",
          instruction: [
            "Use fetch_document to read the user's library documents relevant to: {input}",
            "",
            "These documents were parsed by Azure Content Understanding, so they carry",
            "structured extracted fields alongside the markdown text.",
            "",
            "Report, using only what the documents actually contain:",
            "1. Which document(s) you used, by title.",
            "2. The key extracted fields and their values.",
            "3. Any field that is missing, empty, or internally inconsistent.",
            "",
            "Quote the supporting text for each field you report. If the library",
            "returns nothing relevant, say so plainly and stop -- do not answer from",
            "general knowledge.",
          ].join("\n"),
          extraTools: [],
        },
        {
          agent: "analyst",
          instruction: [
            "Here is a field extraction report:",
            "",
            "{previous}",
            "",
            "Assess its reliability. For each extracted value, state whether the quoted",
            "supporting text actually supports it. Flag every value that is unsupported,",
            "ambiguous, or contradicted. Recompute any arithmetic with the calculator",
            "rather than trusting a number that was read off the page.",
            "",
            "End with an explicit confidence call: high, medium, or low, and why.",
          ].join("\n"),
          extraTools: ["calculator"],
        },
        {
          agent: "writer",
          instruction: [
            "Write the final review for: {input}",
            "",
            "Base it strictly on this assessment:",
            "",
            "{previous}",
            "",
            "Structure: a two-sentence summary, then the verified findings, then a",
            "clearly separated section for anything unverified or missing. Keep every",
            "caveat the assessment raised -- do not smooth them away. If confidence was",
            "low, say that in the opening summary rather than burying it.",
          ].join("\n"),
          extraTools: [],
        },
      ],
    },
  },
  {
    id: "mistral-ocr-extract",
    blurb:
      "Mistral OCR / Document AI: pull a clean, structured record out of scanned or image-based pages.",
    workflow: {
      name: "ocr-extract",
      displayName: "Extract from scans (Mistral OCR)",
      description:
        "Reads a document already processed by Mistral OCR or Mistral Document AI and turns the recovered page text into a structured, transcription-flagged record.",
      enabled: true,
      steps: [
        {
          agent: "researcher",
          instruction: [
            "Use fetch_document to read the user's library documents relevant to: {input}",
            "",
            "These pages came from Mistral OCR, so the text was recovered from images or",
            "scans and may contain transcription errors. Tables are recovered as markdown.",
            "",
            "Transcribe what is present, preserving table structure. Then list every",
            "passage you believe is an OCR artifact rather than the real content --",
            "garbled words, impossible numbers, broken table rows, characters that are",
            "commonly confused (0/O, 1/l, 5/S).",
            "",
            "Do not silently correct anything. Quote the text as it appears and mark",
            "your suspicion separately. If the library returns nothing relevant, say so",
            "and stop.",
          ].join("\n"),
          extraTools: [],
        },
        {
          agent: "analyst",
          instruction: [
            "Here is an OCR transcription with suspected artifacts flagged:",
            "",
            "{previous}",
            "",
            "Produce the structured record the user asked for: {input}",
            "",
            "Rules:",
            "- Every field must trace to quoted transcribed text.",
            "- Use the calculator for any total, subtotal, or cross-check, and say",
            "  explicitly when a stated total does not match the computed one -- that",
            "  mismatch is usually where an OCR error is hiding.",
            "- Where a value sits on flagged text, mark it 'needs verification' and give",
            "  the plausible readings instead of picking one.",
            "- Never invent a value to complete the record. Leave it missing.",
          ].join("\n"),
          extraTools: ["calculator"],
        },
      ],
    },
  },
];

/** Look up a template by its picker id. */
export function templateById(id: string): WorkflowTemplate | undefined {
  return WORKFLOW_TEMPLATES.find((t) => t.id === id);
}
