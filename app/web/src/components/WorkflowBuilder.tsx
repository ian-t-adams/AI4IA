"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "@/lib/api";
import type { AgentSummary, Workflow } from "@/lib/types";
import {
  INPUT_TOKEN,
  MAX_DESCRIPTION_LEN,
  MAX_DISPLAY_NAME_LEN,
  MAX_INSTRUCTION_LEN,
  MAX_RUN_INPUT_LEN,
  MAX_STEPS,
  nameError,
} from "@/lib/studio";
import { HelpTooltip } from "./HelpTooltip";
import { checkRow, ghostBtn, iconBtn, inputStyle, labelStyle, primaryBtn } from "./builderStyles";

// Client-only stable key so React can track step rows across reorder/remove
// without the instruction/agent values "travelling" to the wrong row.
function genKey(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `k-${Math.random().toString(36).slice(2)}-${Date.now()}`;
  }
}

interface StepRow {
  key: string;
  agent: string;
  instruction: string;
}

interface WorkflowForm {
  name: string;
  displayName: string;
  description: string;
  enabled: boolean;
  steps: StepRow[];
}

function blankForm(firstAgent: string): WorkflowForm {
  return {
    name: "",
    displayName: "",
    description: "",
    enabled: true,
    steps: [{ key: genKey(), agent: firstAgent, instruction: "" }],
  };
}

function formFrom(w: Workflow): WorkflowForm {
  return {
    name: w.name,
    displayName: w.displayName,
    description: w.description,
    enabled: w.enabled,
    steps: w.steps.length
      ? w.steps.map((s) => ({ key: genKey(), agent: s.agent, instruction: s.instruction }))
      : [{ key: genKey(), agent: "", instruction: "" }],
  };
}

export function WorkflowBuilder({
  agents,
  runModel,
  onRun,
}: {
  agents: AgentSummary[];
  runModel: string | null;
  onRun: (sessionId: string) => void;
}) {
  const [mine, setMine] = useState<Workflow[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const firstAgentName = agents[0]?.name ?? "";
  const [form, setForm] = useState<WorkflowForm>(() => blankForm(firstAgentName));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Run state (a saved workflow can be run from its list row).
  const [runTarget, setRunTarget] = useState<string | null>(null);
  const [runInput, setRunInput] = useState("");
  const [running, setRunning] = useState(false);

  const agentNames = useMemo(() => new Set(agents.map((a) => a.name)), [agents]);
  const agentsByName = useMemo(() => new Map(agents.map((a) => [a.name, a])), [agents]);

  const refreshMine = useCallback(async () => {
    try {
      setMine(await api.listWorkflows());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refreshMine();
  }, [refreshMine]);

  const startNew = useCallback(() => {
    setEditing(null);
    setForm(blankForm(firstAgentName));
    setError(null);
  }, [firstAgentName]);

  const startEdit = useCallback((w: Workflow) => {
    setEditing(w.name);
    setForm(formFrom(w));
    setError(null);
  }, []);

  // --- step array ops (all immutable, keyed by client id) ---
  const setSteps = useCallback(
    (fn: (s: StepRow[]) => StepRow[]) => setForm((f) => ({ ...f, steps: fn(f.steps) })),
    [],
  );
  const addStep = useCallback(
    () =>
      setSteps((s) =>
        s.length >= MAX_STEPS
          ? s
          : [...s, { key: genKey(), agent: firstAgentName, instruction: "" }],
      ),
    [setSteps, firstAgentName],
  );
  const removeStep = useCallback(
    (key: string) => setSteps((s) => (s.length <= 1 ? s : s.filter((r) => r.key !== key))),
    [setSteps],
  );
  const moveStep = useCallback(
    (key: string, dir: -1 | 1) =>
      setSteps((s) => {
        const i = s.findIndex((r) => r.key === key);
        const j = i + dir;
        if (i < 0 || j < 0 || j >= s.length) return s;
        const next = [...s];
        [next[i], next[j]] = [next[j], next[i]];
        return next;
      }),
    [setSteps],
  );
  const patchStep = useCallback(
    (key: string, patch: Partial<Pick<StepRow, "agent" | "instruction">>) =>
      setSteps((s) => s.map((r) => (r.key === key ? { ...r, ...patch } : r))),
    [setSteps],
  );

  // First step must reference {input} — recomputed from the CURRENT order, so a
  // reorder that moves a non-{input} step into position 1 is flagged correctly.
  const firstMissingInput =
    form.steps.length > 0 && !form.steps[0].instruction.includes(INPUT_TOKEN);

  const submit = useCallback(async () => {
    setError(null);
    if (!editing) {
      const ne = nameError(form.name);
      if (ne) {
        setError(ne);
        return;
      }
    }
    if (form.steps.length === 0) {
      setError("Add at least one step.");
      return;
    }
    if (form.steps.some((s) => !s.agent.trim() || !s.instruction.trim())) {
      setError("Every step needs an agent and an instruction.");
      return;
    }
    if (firstMissingInput) {
      setError(`The first step's instruction must include ${INPUT_TOKEN}.`);
      return;
    }
    const steps = form.steps.map((s) => ({ agent: s.agent, instruction: s.instruction }));
    const body = {
      displayName: form.displayName || null,
      description: form.description,
      steps,
      enabled: form.enabled,
    };
    setBusy(true);
    try {
      const saved = editing
        ? await api.updateWorkflow(editing, body)
        : await api.createWorkflow({ name: form.name, ...body });
      await refreshMine();
      setEditing(saved.name);
      setForm(formFrom(saved));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [editing, form, firstMissingInput, refreshMine]);

  const remove = useCallback(
    async (name: string) => {
      setBusy(true);
      setError(null);
      try {
        await api.deleteWorkflow(name);
        await refreshMine();
        if (editing === name) startNew();
        if (runTarget === name) setRunTarget(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [editing, refreshMine, startNew, runTarget],
  );

  const doRun = useCallback(async () => {
    if (!runTarget || !runInput.trim()) return;
    setError(null);
    // Validate before creating a session so a rejected run never leaves an empty
    // "Run: …" session behind.
    if (!runModel) {
      setError("Pick a model in the chat header before running.");
      return;
    }
    if (runInput.length > MAX_RUN_INPUT_LEN) {
      setError(`Input must be ≤ ${MAX_RUN_INPUT_LEN} characters.`);
      return;
    }
    setRunning(true);
    try {
      const session = await api.createSession({
        title: `Run: ${runTarget}`,
        model: runModel,
      });
      await api.runWorkflow(runTarget, {
        sessionId: session.id,
        input: runInput,
        model: runModel,
      });
      setRunInput("");
      setRunTarget(null);
      onRun(session.id); // hand off to chat so the user sees the result (or the
      // persisted failure message if the run returned ok=false)
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [runTarget, runInput, runModel, onRun]);

  function agentOptions(current: string) {
    // Preserve a stored agent name that's no longer in the composed catalog so
    // editing a workflow never silently drops a referenced agent.
    const missing = current && !agentNames.has(current);
    return (
      <>
        <option value="">Select agent…</option>
        {missing && <option value={current}>{current} (missing)</option>}
        {agents.map((a) => (
          <option key={a.name} value={a.name} title={a.description || undefined}>
            {a.displayName || a.name}
          </option>
        ))}
      </>
    );
  }

  return (
    <div style={{ display: "flex", gap: 20, minHeight: 0, flex: 1 }}>
      <div style={{ width: 200, flexShrink: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <button onClick={startNew} disabled={busy} style={primaryBtn}>
          + New workflow
        </button>
        <ul style={{ listStyle: "none", margin: 0, padding: 0, overflowY: "auto", flex: 1 }}>
          {mine.length === 0 && (
            <li style={{ color: "var(--fg-muted)", fontSize: "0.85em", padding: 8 }}>
              No workflows yet.
            </li>
          )}
          {mine.map((w) => (
            <li key={w.id} style={{ display: "flex", flexDirection: "column", gap: 2, marginBottom: 4 }}>
              <div style={{ display: "flex", alignItems: "center" }}>
                <button
                  onClick={() => startEdit(w)}
                  aria-current={editing === w.name ? "true" : undefined}
                  style={{
                    flex: 1,
                    textAlign: "left",
                    padding: "8px 10px",
                    borderRadius: 8,
                    border: "none",
                    background: editing === w.name ? "var(--bg)" : "transparent",
                    color: "var(--fg)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {w.displayName || w.name}
                  {!w.enabled && <span style={{ color: "var(--fg-muted)" }}> (off)</span>}
                </button>
                <button
                  onClick={() => remove(w.name)}
                  disabled={busy}
                  aria-label={`Delete ${w.name}`}
                  title="Delete"
                  style={iconBtn}
                >
                  ✕
                </button>
              </div>
              <button
                onClick={() => {
                  setRunTarget((cur) => (cur === w.name ? null : w.name));
                  setRunInput("");
                }}
                disabled={busy || !w.enabled}
                style={{ ...ghostBtn, margin: "0 6px" }}
              >
                {runTarget === w.name ? "Cancel run" : "▶ Run"}
              </button>
              {runTarget === w.name && (
                <div style={{ padding: "4px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
                  <textarea
                    aria-label={`Input for ${w.name}`}
                    placeholder="Input for the first step…"
                    value={runInput}
                    rows={3}
                    maxLength={MAX_RUN_INPUT_LEN}
                    onChange={(e) => setRunInput(e.target.value)}
                    style={{ ...inputStyle, resize: "vertical" }}
                  />
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <button
                      onClick={doRun}
                      disabled={running || !runInput.trim() || !runModel}
                      style={primaryBtn}
                    >
                      {running ? "Running…" : "Run in new chat"}
                    </button>
                    {!runModel && (
                      <span style={{ ...labelStyle, margin: 0 }}>
                        Pick a model in the chat header first.
                      </span>
                    )}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div style={{ flex: 1, minWidth: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 14 }}>
        <h3 style={{ margin: 0, fontSize: "1em" }}>
          {editing ? `Edit ${editing}` : "New workflow"}
        </h3>

        <div>
          <label style={labelStyle} htmlFor="wf-name">Name</label>
          <input
            id="wf-name"
            value={form.name}
            disabled={!!editing}
            placeholder="e.g. summarize"
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            style={{ ...inputStyle, opacity: editing ? 0.6 : 1 }}
          />
          {editing && (
            <p style={{ ...labelStyle, marginTop: 4 }}>
              Name is the stable ID and can&apos;t be changed.
            </p>
          )}
        </div>

        <div>
          <label style={labelStyle} htmlFor="wf-display">Display name</label>
          <input
            id="wf-display"
            value={form.displayName}
            maxLength={MAX_DISPLAY_NAME_LEN}
            onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="wf-desc">Description</label>
          <input
            id="wf-desc"
            value={form.description}
            maxLength={MAX_DESCRIPTION_LEN}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <span style={labelStyle}>Steps ({form.steps.length}/{MAX_STEPS})</span>
              <HelpTooltip label="About workflow steps" size="sm">
                Steps run in order. Each is at least one model call, but a step whose agent
                uses tools can take up to three (an initial tool-calling attempt, a follow-up
                with the tool result, and a forced final answer if it keeps requesting tools)
                — every extra step and tool call adds latency and cost. Only the immediately
                prior step&apos;s output is passed forward via {" "}
                {"{previous}"}, truncated to 8,000 characters — not the full conversation
                history.
              </HelpTooltip>
            </span>
            <button onClick={addStep} disabled={form.steps.length >= MAX_STEPS} style={ghostBtn}>
              + Add step
            </button>
          </div>
          <p style={{ ...labelStyle, margin: 0 }}>
            Use {INPUT_TOKEN} for the run input and {"{previous}"} for the prior step&apos;s
            output. The first step must include {INPUT_TOKEN}.
          </p>
          {form.steps.map((s, i) => {
            const selectedAgent = agentsByName.get(s.agent);
            return (
              <div
                key={s.key}
                style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 10, display: "flex", flexDirection: "column", gap: 8 }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <strong style={{ fontSize: "0.85em" }}>Step {i + 1}</strong>
                  <select
                    aria-label={`Step ${i + 1} agent`}
                    value={s.agent}
                    onChange={(e) => patchStep(s.key, { agent: e.target.value })}
                    style={{ ...inputStyle, width: "auto", flex: 1 }}
                  >
                    {agentOptions(s.agent)}
                  </select>
                  <button onClick={() => moveStep(s.key, -1)} disabled={i === 0} aria-label="Move up" style={iconBtn}>↑</button>
                  <button onClick={() => moveStep(s.key, 1)} disabled={i === form.steps.length - 1} aria-label="Move down" style={iconBtn}>↓</button>
                  <button
                    onClick={() => removeStep(s.key)}
                    disabled={form.steps.length <= 1}
                    aria-label="Remove step"
                    style={iconBtn}
                  >
                    ✕
                  </button>
                </div>
                {selectedAgent?.description && (
                  <p style={{ ...labelStyle, margin: 0 }}>{selectedAgent.description}</p>
                )}
                <textarea
                  aria-label={`Step ${i + 1} instruction`}
                  value={s.instruction}
                  maxLength={MAX_INSTRUCTION_LEN}
                  rows={2}
                  placeholder={i === 0 ? `Answer this: ${INPUT_TOKEN}` : "Refine: {previous}"}
                  onChange={(e) => patchStep(s.key, { instruction: e.target.value })}
                  style={{ ...inputStyle, resize: "vertical" }}
                />
                {i === 0 && firstMissingInput && (
                  <p role="alert" style={{ color: "var(--danger)", fontSize: "0.8em", margin: 0 }}>
                    First step must include {INPUT_TOKEN}.
                  </p>
                )}
              </div>
            );
          })}
        </div>

        <label style={checkRow}>
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
          />
          Enabled
        </label>

        {error && (
          <p role="alert" style={{ color: "var(--danger)", fontSize: "0.85em", margin: 0 }}>
            {error}
          </p>
        )}

        <div>
          <button onClick={submit} disabled={busy} style={primaryBtn}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create workflow"}
          </button>
        </div>
      </div>
    </div>
  );
}
