// @vitest-environment node
import { describe, expect, it } from "vitest";

import { deriveSteps, pendingSteps } from "./workflowRun";

const TWO = [{ label: "Research Assistant" }, { label: "Helper" }];

describe("pendingSteps", () => {
  it("labels every row and numbers them from zero", () => {
    const rows = pendingSteps(TWO, "pending");
    expect(rows.map((r) => [r.index, r.agentLabel, r.state])).toEqual([
      [0, "Research Assistant", "pending"],
      [1, "Helper", "pending"],
    ]);
    expect(rows.every((r) => r.text === null && r.error === null)).toBe(true);
  });
});

describe("deriveSteps", () => {
  it("puts the output on the last step only, because that is the only one reported", () => {
    const rows = deriveSteps(TWO, true, "the write-up");
    expect(rows.map((r) => r.state)).toEqual(["succeeded", "succeeded"]);
    // Giving step 1 this text would attribute the final step's words to a step
    // that may have said something entirely different.
    expect(rows[0].text).toBeNull();
    expect(rows[1].text).toBe("the write-up");
  });

  it("attributes a prefixed failure, and marks everything after it as never started", () => {
    const rows = deriveSteps(
      [{ label: "a" }, { label: "b" }, { label: "c" }],
      false,
      "Step 2: the model refused",
    );
    expect(rows.map((r) => r.state)).toEqual(["succeeded", "failed", "skipped"]);
    expect(rows[1].error).toBe("Step 2: the model refused");
    expect(rows[0].error).toBeNull();
  });

  it("claims nothing per-step when the failure came from outside a step", () => {
    // runner.py prefixes every FATAL STEP error. A message with no prefix came
    // from somewhere else (session lookup, model resolution), so blaming a step
    // would be a fabrication — and "step 1 failed" sends the user to rewrite an
    // instruction that is fine.
    const rows = deriveSteps(TWO, false, "workflow is disabled");
    expect(rows.map((r) => r.state)).toEqual(["unknown", "unknown"]);
    expect(rows[0].error).toBe("workflow is disabled");
  });

  it("does not trust a step number that cannot exist", () => {
    // A prefix wider than the workflow means the two disagree about the shape of
    // the run. Indexing on it would either throw or silently mark the wrong row.
    const rows = deriveSteps(TWO, false, "Step 7: exploded");
    expect(rows.map((r) => r.state)).toEqual(["unknown", "unknown"]);
    expect(rows[0].error).toBe("Step 7: exploded");
  });

  it("survives a workflow with no steps rather than writing past the end", () => {
    expect(deriveSteps([], true, "anything")).toEqual([]);
    expect(deriveSteps([], false, "Step 1: nope")).toEqual([]);
  });

  it("only matches the prefix at the start, so quoted text cannot reattribute a failure", () => {
    const rows = deriveSteps(TWO, false, 'The agent replied "Step 2: done" and then failed');
    expect(rows.map((r) => r.state)).toEqual(["unknown", "unknown"]);
  });
});


describe("structured workflow activity", () => {
  it("preserves later steps after an earlier error rather than guessing they were skipped", () => {
    const steps = deriveSteps([{ label: "a" }, { label: "b" }, { label: "c" }], false, "Step 2: unavailable", [
      { kind: "workflow_step", label: "Step 1: a", detail: "completed" },
      { kind: "workflow_error", label: "Step 2: b", detail: "failed" },
      { kind: "workflow_step", label: "Step 3: c", detail: "completed" },
    ]);
    expect(steps.map((step) => step.state)).toEqual(["succeeded", "failed", "succeeded"]);
    expect(steps[1].error).toBe("Step 2: unavailable");
  });

  it("does not turn a bounded trace's missing steps into claimed successes or skips", () => {
    const steps = deriveSteps(TWO, false, "Step 2: stop", [{ kind: "workflow_error", label: "Step 2: b", detail: "cancelled" }]);
    expect(steps.map((step) => step.state)).toEqual(["unknown", "cancelled"]);
  });

  it("never treats text from a tool as workflow execution metadata", () => {
    const steps = deriveSteps(TWO, false, "Connection interrupted", [{ kind: "tool_result", label: "Step 2: helper", detail: "completed" }]);
    expect(steps.map((step) => step.state)).toEqual(["unknown", "unknown"]);
  });
});
