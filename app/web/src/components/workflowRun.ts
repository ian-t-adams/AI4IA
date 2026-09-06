import type { ActivityStep, ExecutionReceipt, Message } from "@/lib/types";

// Pure run-state logic for the workflow runner, kept out of the component so it
// can be tested directly — and, because it is plain TypeScript rather than JSX,
// syntax-checked without a bundler.
//
// Modern run records carry authoritative workflow-step activity markers and
// independent receipts. Always prefer those markers, preserving evidence of a
// later step even when an earlier one failed. Older records contain only the
// final text and an ok/failed flag: the strict `Step {index+1}:` fatal-error
// prefix is a compatibility path, never a reason to parse arbitrary tool text.
// Missing or bounded evidence remains unknown rather than fabricated success.

export type RunStepState = "succeeded" | "failed" | "skipped" | "pending" | "unknown" | "cancelled";

export interface RunStepView {
  index: number;
  agentLabel: string;
  state: RunStepState;
  /** Output text, only when the transcript makes it attributable to this step. */
  text: string | null;
  error: string | null;
}

export interface RunEvidence {
  executionReceipt?: ExecutionReceipt | null;
  activity?: ActivityStep[] | null;
  workflowStepReceipts?: ExecutionReceipt[] | null;
  evidenceError?: string | null;
}

export type RunState =
  | { phase: "idle" }
  | { phase: "running"; steps: RunStepView[] }
  | ({ phase: "succeeded"; sessionId: string; steps: RunStepView[]; elapsedMs: number } & RunEvidence)
  | ({ phase: "failed"; sessionId: string | null; steps: RunStepView[] } & RunEvidence)
  | ({ phase: "cancelled"; sessionId: string; runId?: string; steps: RunStepView[] } & RunEvidence)
  | ({ phase: "timedOut"; sessionId: string; steps: RunStepView[] } & RunEvidence)
  | ({ phase: "unknown"; sessionId: string; steps: RunStepView[]; error: string } & RunEvidence);

/** A run that has finished (or given up), i.e. one that has something to show. */
export type SettledRunState = Exclude<RunState, { phase: "idle" } | { phase: "running" }>;

export function pendingSteps(
  stepAgents: { label: string }[],
  state: RunStepState,
): RunStepView[] {
  return stepAgents.map((s, index) => ({
    index,
    agentLabel: s.label,
    state,
    text: null,
    error: null,
  }));
}

const STEP_PREFIX = /^Step (\d+):/;

export function deriveSteps(
  stepAgents: { label: string }[],
  ok: boolean,
  text: string,
  activity?: readonly ActivityStep[] | null,
): RunStepView[] {
  const base = pendingSteps(stepAgents, "unknown");
  // Only server-produced workflow markers can override the legacy text protocol.
  // Tool result text that happens to say "Step 2:" is never execution metadata.
  const recorded = (activity ?? []).flatMap((entry) => {
    if (entry.kind !== "workflow_step" && entry.kind !== "workflow_error") return [];
    const match = STEP_PREFIX.exec(entry.label);
    const index = match ? Number(match[1]) - 1 : -1;
    return index >= 0 && index < base.length ? [{ entry, index }] : [];
  });
  if (recorded.length) {
    const failedPrefix = STEP_PREFIX.exec(text);
    for (const { entry, index } of recorded) {
      base[index].state = entry.kind === "workflow_step" ? "succeeded"
        : entry.detail === "cancelled" ? "cancelled" : "failed";
      if (entry.kind === "workflow_error") {
        base[index].error = failedPrefix && Number(failedPrefix[1]) - 1 === index
          ? text : entry.detail || "This step did not complete.";
      }
    }
    // Do not erase evidence of steps that continued after an earlier error.
    // Unrecorded steps remain unknown; a bounded trace isn't proof of skipping.
    if (ok && base.at(-1)?.state === "succeeded") base[base.length - 1].text = text;
    else if (!ok && !recorded.some(({ entry }) => entry.kind === "workflow_error") && base.length) base[0].error = text;
    return base;
  }

  if (ok) {
    for (const s of base) s.state = "succeeded";
    // Only the final step's output is reported, so it is the only one that can
    // honestly carry a body. Intermediate steps stay bodiless rather than being
    // shown text they may not have produced.
    if (base.length) base[base.length - 1].text = text;
    return base;
  }

  const match = STEP_PREFIX.exec(text);
  const failed = match ? Number(match[1]) - 1 : -1;
  if (failed < 0 || failed >= base.length) {
    // Unattributable: leave every state "unknown" and surface the message once,
    // rather than asserting a step failed when the evidence does not say so.
    if (base.length) base[0].error = text;
    return base;
  }

  base.forEach((s, i) => {
    s.state = i < failed ? "succeeded" : i === failed ? "failed" : "skipped";
  });
  base[failed].error = text;
  return base;
}

export function evidenceFromMessage(message: Message | null | undefined): RunEvidence {
  return message ? {
    executionReceipt: message.executionReceipt,
    workflowStepReceipts: message.workflowStepReceipts,
    activity: message.steps,
  } : {};
}

export function resultFromMessage(
  stepAgents: { label: string }[],
  message: Message,
  ok: boolean,
  elapsedMs: number,
): SettledRunState {
  const cancelled = message.status === "cancelled" || message.workflowConsentRevoked === true;
  const succeeded = ok && !cancelled && message.status !== "error";
  const common = {
    sessionId: message.sessionId,
    steps: deriveSteps(stepAgents, succeeded, message.content, message.steps),
    ...evidenceFromMessage(message),
  };
  if (cancelled) return { ...common, phase: "cancelled", runId: message.workflowRunId ?? undefined };
  return succeeded ? { ...common, phase: "succeeded", elapsedMs } : { ...common, phase: "failed" };
}
