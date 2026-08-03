// Pure run-state logic for the workflow runner, kept out of the component so it
// can be tested directly — and, because it is plain TypeScript rather than JSX,
// syntax-checked without a bundler.
//
// The interesting part is `deriveSteps`. A workflow run answers with a single
// blob of text and an ok/failed flag, which cannot say *which* step broke. That
// matters: "the workflow failed" sends you re-reading six instructions, whereas
// "step 4 failed, 1-3 succeeded, 5-6 never started" points at one.
//
// runner.py prefixes every fatal step error with `Step {index+1}: ` — every
// fatal path, no exceptions — so the attribution is recoverable from the text
// with no backend change. Where the prefix is absent the failure came from
// outside a step, and this deliberately claims nothing per-step rather than
// blaming an innocent row.

export type RunStepState = "succeeded" | "failed" | "skipped" | "pending" | "unknown";

export interface RunStepView {
  index: number;
  agentLabel: string;
  state: RunStepState;
  /** Output text, only when the transcript makes it attributable to this step. */
  text: string | null;
  error: string | null;
}

export type RunState =
  | { phase: "idle" }
  | { phase: "running"; steps: RunStepView[] }
  | { phase: "succeeded"; sessionId: string; steps: RunStepView[]; elapsedMs: number }
  | { phase: "failed"; sessionId: string | null; steps: RunStepView[] }
  | { phase: "timedOut"; sessionId: string; steps: RunStepView[] };

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
): RunStepView[] {
  const base = pendingSteps(stepAgents, "unknown");

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
