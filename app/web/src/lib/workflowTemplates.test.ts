/**
 * Starter templates must satisfy the *server's* rules, not just look reasonable.
 *
 * These assertions mirror `app/api/src/ai4ia_api/workflows/models.py` and
 * `agents/tool_exec.py`. They exist because both failure modes here are silent:
 * a template that violates a server limit 422s only when a user tries to save
 * it, and a template naming a chat-only tool saves and runs *fine* while the
 * step quietly never gets the tool -- the model then narrates work it did not
 * do and the run is persisted as a success.
 */
import { describe, expect, it } from "vitest";

import {
  CHAT_ONLY_TOOLS,
  WORKFLOW_STEP_TOOLS,
  WORKFLOW_TEMPLATES,
  templateById,
} from "./workflowTemplates";

// Mirrors workflows/models.py.
const MAX_STEPS = 6;
const MAX_NAME_LEN = 32;
const MAX_DISPLAY_NAME_LEN = 80;
const MAX_DESCRIPTION_LEN = 280;
const MAX_INSTRUCTION_LEN = 4000;
const MAX_STEP_TOOLS = 12;
// Mirrors agents/user_agents.py NAME_RE.
const NAME_RE = /^[a-z](?:[a-z0-9_.-]{0,30}[a-z0-9_])?$/;
// Mirrors data/agents.json.
const CURATED_AGENTS = new Set(["general", "coder", "researcher", "writer", "analyst"]);

describe("workflow templates", () => {
  it("ships the two document templates", () => {
    // Non-vacuity: every per-template assertion below is meaningless if the
    // list is empty, and the two documented ones are the point of the feature.
    expect(WORKFLOW_TEMPLATES.length).toBeGreaterThanOrEqual(2);
    const ids = WORKFLOW_TEMPLATES.map((t) => t.id);
    expect(ids).toContain("cu-document-review");
    expect(ids).toContain("mistral-ocr-extract");
  });

  it("has unique ids and unique workflow names", () => {
    const ids = WORKFLOW_TEMPLATES.map((t) => t.id);
    const names = WORKFLOW_TEMPLATES.map((t) => t.workflow.name);
    expect(new Set(ids).size).toBe(ids.length);
    // Names are the per-user Cosmos id, so two templates sharing one would make
    // saving the second overwrite or 409 against the first.
    expect(new Set(names).size).toBe(names.length);
  });

  describe.each(WORKFLOW_TEMPLATES)("$id", (template) => {
    const wf = template.workflow;

    it("satisfies the server's name and text limits", () => {
      expect(wf.name).toMatch(NAME_RE);
      expect(wf.name.length).toBeLessThanOrEqual(MAX_NAME_LEN);
      expect(wf.displayName ?? "").not.toHaveLength(0);
      expect((wf.displayName ?? "").length).toBeLessThanOrEqual(MAX_DISPLAY_NAME_LEN);
      expect(wf.description.length).toBeLessThanOrEqual(MAX_DESCRIPTION_LEN);
      expect(template.blurb.length).toBeGreaterThan(0);
    });

    it("has a runnable step list", () => {
      expect(wf.steps.length).toBeGreaterThan(0);
      expect(wf.steps.length).toBeLessThanOrEqual(MAX_STEPS);
      for (const step of wf.steps) {
        expect(CURATED_AGENTS.has(step.agent)).toBe(true);
        expect(step.instruction.length).toBeGreaterThan(0);
        expect(step.instruction.length).toBeLessThanOrEqual(MAX_INSTRUCTION_LEN);
        expect(step.extraTools.length).toBeLessThanOrEqual(MAX_STEP_TOOLS);
      }
    });

    it("threads {input} and {previous} so no step ignores its predecessor", () => {
      // Step 1 has no predecessor, so it must consume the run input; every later
      // step must consume the prior output or the pipeline is not a pipeline --
      // it is N independent turns billed as one run.
      expect(wf.steps[0].instruction).toContain("{input}");
      for (const step of wf.steps.slice(1)) {
        expect(step.instruction).toContain("{previous}");
      }
    });

    it("never names a tool a workflow step cannot actually run", () => {
      const runnable = new Set<string>(WORKFLOW_STEP_TOOLS);
      for (const step of wf.steps) {
        for (const tool of step.extraTools) {
          expect(
            runnable.has(tool),
            `step targeting ${step.agent} requests ${tool}, which a workflow step cannot run`,
          ).toBe(true);
        }
      }
    });

    it("never requests fetch_document as an extra tool", () => {
      // It is injected by the server from the retrieval service whenever the
      // library is on, and is not user-attachable. Listing it would be rejected
      // as an unknown tool while looking like the thing that makes this work.
      for (const step of wf.steps) {
        expect(step.extraTools).not.toContain("fetch_document");
      }
    });

    it("tells the model to read the library rather than answer from memory", () => {
      // The document templates are worthless if step 1 does not actually ground
      // itself in the user's documents.
      expect(wf.steps[0].instruction).toContain("fetch_document");
    });
  });

  it("classifies chat-only tools as unrunnable in a workflow", () => {
    // Control for "never names a tool a workflow step cannot run": that test is
    // vacuous if the two sets happen to overlap, since then any tool would pass.
    const runnable = new Set<string>(WORKFLOW_STEP_TOOLS);
    for (const tool of CHAT_ONLY_TOOLS) {
      expect(runnable.has(tool)).toBe(false);
    }
    expect(CHAT_ONLY_TOOLS.length).toBeGreaterThan(0);
  });

  it("resolves templates by id and rejects unknown ones", () => {
    expect(templateById("cu-document-review")?.workflow.name).toBe("cu-doc-review");
    expect(templateById("nope")).toBeUndefined();
  });
});
