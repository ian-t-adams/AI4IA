"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "@/lib/api";
import type { AgentSummary, ToolCatalogItem, Workflow, WorkflowRunStatus } from "@/lib/types";
import type { LibraryDocument } from "@/lib/library";
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
import { TOOL_LABELS } from "@/lib/toolHelp";
import { WorkflowDisclosure } from "./WorkflowDisclosure";
import { WorkflowRunReport } from "./WorkflowRunReport";
import { deriveSteps, pendingSteps, type RunState } from "./workflowRun";
import { useLibraryConfig } from "./LibraryProvider";
import {
  stepCapabilities,
  STEP_ATTACHABLE_TOOLS,
  type CapabilityChip,
} from "./workflowCapabilities";
import { checkRow, ghostBtn, inputStyle, labelStyle, primaryBtn, secondaryBtn } from "./builderStyles";

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
  extraTools: string[];
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
    steps: [{ key: genKey(), agent: firstAgent, instruction: "", extraTools: [] }],
  };
}

function formFrom(w: Workflow): WorkflowForm {
  return {
    name: w.name,
    displayName: w.displayName,
    description: w.description,
    enabled: w.enabled,
    steps: w.steps.length
      ? w.steps.map((s) => ({
          key: genKey(),
          agent: s.agent,
          instruction: s.instruction,
          // Absent on workflows saved before per-step tools existed.
          extraTools: s.extraTools ?? [],
        }))
      : [{ key: genKey(), agent: "", instruction: "", extraTools: [] }],
  };
}

// Poll cadence for a scheduled durable run. The ceiling is a UI patience budget,
// NOT a run deadline: giving up here abandons the poll, never the orchestration,
// which keeps going server-side and writes its assistant turn to the session
// whenever it finishes. The API enforces the real timeout.
const RUN_POLL_INTERVAL_MS = 1500;
const RUN_POLL_MAX_ATTEMPTS = 80; // ~2 minutes
const POLL_BUDGET_SECONDS = Math.round((RUN_POLL_INTERVAL_MS * RUN_POLL_MAX_ATTEMPTS) / 1000);

// sessions/models.py caps a session's document scope. Mirrored so the UI stops
// at the limit instead of letting the request 422.
const MAX_DOCS_PER_RUN = 20;

// Polls until the run reaches a terminal state. Returns null if the patience
// budget runs out first, which the caller reports as "still running" rather than
// as a failure — the two are genuinely different, and conflating them would tell
// the user a healthy long run had broken.
//
// `isCancelled` is checked on both sides of every await: without it an unmounted
// panel kept polling for up to two minutes, calling setState on a dead component
// and holding a request in flight per navigation away from a running workflow.
async function pollRun(
  runId: string,
  onStatus: (status: string) => void,
  isCancelled: () => boolean = () => false,
): Promise<WorkflowRunStatus | null> {
  for (let attempt = 0; attempt < RUN_POLL_MAX_ATTEMPTS; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, RUN_POLL_INTERVAL_MS));
    if (isCancelled()) return null;
    const status = await api.getWorkflowRun(runId);
    if (isCancelled()) return null;
    onStatus(status.status);
    if (api.isTerminalRunStatus(status.status)) return status;
  }
  return null;
}

const DOC_STATUS_LABEL: Record<LibraryDocument["status"], string> = {
  pending: "Pending",
  stored: "Pending",
  analyzing: "Analyzing…",
  ready: "Ready",
  failed: "Failed",
};

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
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
  const [tab, setTab] = useState<"build" | "run">("build");

  const [runInput, setRunInput] = useState("");
  const [running, setRunning] = useState(false);
  // Server-reported: whether this deployment can honour a durable run at all.
  // The control is hidden (not merely disabled) when it cannot, so the UI never
  // offers an option whose only possible outcome is a 422.
  const [durableAvailable, setDurableAvailable] = useState(false);
  const [runDurable, setRunDurable] = useState(false);
  // Surfaced while polling a scheduled run so the button is not silently busy
  // for what may be minutes.
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [runState, setRunState] = useState<RunState>({ phase: "idle" });
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());

  const library = useLibraryConfig();
  const [docs, setDocs] = useState<LibraryDocument[]>([]);
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([]);

  // Per-agent tool catalog, so a step whose agent cannot be resolved can say so.
  // "error" is a recorded outcome, not an absence: an empty strip reads as "this
  // step has no tools", which is a wrong answer rather than a missing one.
  const [catalogs, setCatalogs] = useState<
    Map<string, { tools: ToolCatalogItem[]; inheritedTools: string[] } | "error">
  >(new Map());
  // Agent names already requested. Held in a ref and mutated only inside the
  // effect below — reading `catalogs` there instead would have to list it as a
  // dependency, and it changes on every fetch, so the effect would re-run
  // forever. Tracking the request rather than the result also means an agent
  // added to a second step while its first fetch is still in flight does not
  // fire a duplicate.
  const requestedAgents = useRef<Set<string>>(new Set());

  // A durable run polls for up to two minutes. Without this the loop kept
  // running after the panel closed — setState on an unmounted component, plus a
  // request in flight per navigation away from a running workflow. Cleanup runs
  // on unmount only; the ref is never reset to true, so a poll started by a
  // previous mount can never resume against a new one.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const agentNames = useMemo(() => new Set(agents.map((a) => a.name)), [agents]);
  const agentsByName = useMemo(() => new Map(agents.map((a) => [a.name, a])), [agents]);
  const agentLabel = useCallback(
    (name: string) => agentsByName.get(name)?.displayName || name,
    [agentsByName],
  );

  const refreshMine = useCallback(async () => {
    try {
      const listed = await api.listWorkflows();
      setMine(listed.workflows);
      setDurableAvailable(listed.durableAvailable);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refreshMine();
  }, [refreshMine]);

  useEffect(() => {
    if (!library.enabled) return;
    void (async () => {
      try {
        setDocs(await api.listLibraryDocuments());
      } catch {
        // Falling back to an empty list hides nothing: whether a run can read
        // documents at all is reported by the capability chips, which read the
        // server's own availability flag rather than this list.
        setDocs([]);
      }
    })();
  }, [library.enabled]);

  // Fetch the tool catalog for each distinct agent referenced by a step.
  // Already-requested agents are skipped, so editing an instruction costs
  // nothing and a six-step workflow using two agents makes two requests.
  const stepAgentKey = form.steps.map((s) => s.agent).join("\u0000");
  useEffect(() => {
    const wanted = new Set(stepAgentKey.split("\u0000").filter(Boolean));
    const missing = [...wanted].filter((n) => !requestedAgents.current.has(n));
    if (missing.length === 0) return;
    for (const name of missing) requestedAgents.current.add(name);
    void (async () => {
      const fetched = await Promise.all(
        missing.map(async (name) => {
          try {
            // agentName alone: the API 422s when both a session id and an agent
            // name are supplied, and again for an unknown or disabled agent —
            // which is a real answer, recorded so the strip can say so.
            return [name, await api.getToolCatalog(null, name)] as const;
          } catch {
            return [name, "error"] as const;
          }
        }),
      );
      setCatalogs((prev) => {
        const next = new Map(prev);
        for (const [name, value] of fetched) next.set(name, value);
        return next;
      });
    })();
  }, [stepAgentKey]);

  const startNew = useCallback(() => {
    setEditing(null);
    setForm(blankForm(firstAgentName));
    setError(null);
    setRunState({ phase: "idle" });
    setTab("build");
  }, [firstAgentName]);

  const startEdit = useCallback((w: Workflow) => {
    setEditing(w.name);
    setForm(formFrom(w));
    setError(null);
    setRunState({ phase: "idle" });
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
          : [...s, { key: genKey(), agent: firstAgentName, instruction: "", extraTools: [] }],
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
  const toggleStepTool = useCallback(
    (key: string, tool: string) =>
      setSteps((s) =>
        s.map((r) =>
          r.key === key
            ? {
                ...r,
                extraTools: r.extraTools.includes(tool)
                  ? r.extraTools.filter((t) => t !== tool)
                  : [...r.extraTools, tool],
              }
            : r,
        ),
      ),
    [setSteps],
  );

  // First step must reference {input} — recomputed from the CURRENT order, so a
  // reorder that moves a non-{input} step into position 1 is flagged correctly.
  const firstMissingInput =
    form.steps.length > 0 && !form.steps[0].instruction.includes(INPUT_TOKEN);

  // The run target is the SAVED workflow, never the in-progress form: a run
  // executes what the server stored, so tracing the form's steps would misreport
  // an unsaved edit as though it had run.
  const runTarget = useMemo(
    () => (editing ? (mine.find((w) => w.name === editing) ?? null) : null),
    [editing, mine],
  );

  // Returns the saved workflow so "Save & run" can run exactly what was just
  // written rather than diffing the form against the server copy to decide
  // whether it is dirty.
  const submit = useCallback(async (): Promise<Workflow | null> => {
    setError(null);
    if (!editing) {
      const ne = nameError(form.name);
      if (ne) {
        setError(ne);
        return null;
      }
    }
    if (form.steps.length === 0) {
      setError("Add at least one step.");
      return null;
    }
    if (form.steps.some((s) => !s.agent.trim() || !s.instruction.trim())) {
      setError("Every step needs an agent and an instruction.");
      return null;
    }
    if (firstMissingInput) {
      setError(`The first step's instruction must include ${INPUT_TOKEN}.`);
      return null;
    }
    const steps = form.steps.map((s) => ({
      agent: s.agent,
      instruction: s.instruction,
      extraTools: s.extraTools,
    }));
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
      // The saved workflow itself, not its name: `doRun` would otherwise look
      // the name up in `mine`, whose value its closure captured BEFORE this
      // save. Save-and-run then executed the previous definition while the
      // result card reported the new one — and returning the object is immune
      // to `refreshMine()` having failed, which a ref would not be.
      return saved;
    } catch (e) {
      setError((e as Error).message);
      return null;
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
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [editing, refreshMine, startNew],
  );

  const doRun = useCallback(
    async (targetName: string, justSaved?: Workflow) => {
      // Prefer the definition handed in by save-and-run: `mine` is whatever this
      // closure captured, which for an edit-then-run is the PRE-save version.
      const target = justSaved ?? mine.find((w) => w.name === targetName);
      if (!target || !runInput.trim()) return;
      setError(null);
      // Validate before creating a session so a rejected run never leaves an
      // empty "Run: …" session behind.
      if (!runModel) {
        setError("Pick a model in the chat header before running.");
        return;
      }
      if (runInput.length > MAX_RUN_INPUT_LEN) {
        setError(`Input must be ≤ ${MAX_RUN_INPUT_LEN} characters.`);
        return;
      }
      const stepAgents = target.steps.map((s) => ({ label: agentLabel(s.agent) }));
      setRunning(true);
      setRunState({ phase: "running", steps: pendingSteps(stepAgents, "pending") });
      const startedAt = Date.now();
      try {
        const session = await api.createSession({
          title: `Run: ${target.displayName || target.name} · ${new Date().toLocaleTimeString()}`,
          model: runModel,
          // Send the key ONLY when non-empty. `[]` is not "no preference" — the
          // API reads `allowed_document_ids is None or bool(...)`, so an empty
          // array switches document reading OFF for the whole run. Omitting it
          // leaves the scope unset, which means every ready document.
          ...(selectedDocIds.length ? { libraryDocumentIds: selectedDocIds } : {}),
        });
        const outcome = await api.runWorkflow(target.name, {
          sessionId: session.id,
          input: runInput,
          model: runModel,
          // Only ever sent when the server said it can honour it, so the request
          // cannot be rejected for asking.
          ...(durableAvailable && runDurable ? { durable: true } : {}),
        });

        if (outcome.scheduled) {
          // A durable run answers before the assistant turn exists, so poll here
          // rather than handing off: the chat view loads a session's messages
          // once and does not watch for later arrivals.
          const status = await pollRun(outcome.run.runId, setRunStatus, () => !mountedRef.current);
          // Unmounted mid-poll: the orchestration is still running server-side
          // and will write its turn to the session, so there is nothing to
          // report and no state worth setting.
          if (!mountedRef.current) return;
          setRunStatus(null);
          if (status === null) {
            setRunState({
              phase: "timedOut",
              sessionId: session.id,
              steps: pendingSteps(stepAgents, "unknown"),
            });
            return;
          }
          const text = status.error ?? status.text ?? "";
          if (status.error || status.ok === false) {
            setRunState({
              phase: "failed",
              sessionId: session.id,
              steps: deriveSteps(stepAgents, false, text || "The workflow reported a failure."),
            });
            return;
          }
          setRunState({
            phase: "succeeded",
            sessionId: session.id,
            steps: deriveSteps(stepAgents, true, text),
            elapsedMs: Date.now() - startedAt,
          });
          return;
        }

        const text = outcome.result.message.content;
        setRunState(
          outcome.result.ok
            ? {
                phase: "succeeded",
                sessionId: outcome.result.sessionId,
                steps: deriveSteps(stepAgents, true, text),
                elapsedMs: Date.now() - startedAt,
              }
            : {
                phase: "failed",
                sessionId: outcome.result.sessionId,
                steps: deriveSteps(stepAgents, false, text || "The workflow reported a failure."),
              },
        );
      } catch (e) {
        // A pre-flight or transport failure is not a run. Keep it in the alert
        // rather than rendering a result card implying steps executed.
        setError((e as Error).message);
        setRunState({ phase: "idle" });
      } finally {
        setRunning(false);
        setRunStatus(null);
      }
    },
    [mine, runInput, runModel, durableAvailable, runDurable, selectedDocIds, agentLabel],
  );

  const saveAndRun = useCallback(async () => {
    const saved = await submit();
    if (saved) await doRun(saved.name, saved);
  }, [submit, doRun]);

  const toggleSection = useCallback((id: string) => {
    setOpenSections((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleDoc = useCallback((id: string) => {
    setSelectedDocIds((prev) =>
      prev.includes(id) ? prev.filter((d) => d !== id) : [...prev, id],
    );
  }, []);

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

  function chipsFor(agentName: string, extra: string[]): CapabilityChip[] | "error" | null {
    if (!agentName) return null;
    const entry = catalogs.get(agentName);
    if (entry === undefined) return null;
    if (entry === "error") return "error";
    return stepCapabilities({
      attached: entry.inheritedTools,
      extra,
      catalog: entry.tools,
      selectedDocCount: selectedDocIds.length,
      agentLabel: agentLabel(agentName),
    });
  }

  // `onFix`, when given, renders the remedy button. The Build tab passes nothing:
  // its checkboxes sit directly below the chips, so a button would be noise. The
  // Run tab's steps are read-only, so there it routes back to Build.
  //
  // It deliberately no longer offers "open Agents to attach tools". That was the
  // dead end this whole feature exists to remove: the memory tools are gated on
  // the step's effective tool list, and a *curated* agent's list cannot be edited
  // in Agents at all — so the old button sent users somewhere that could not help.
  function renderChips(
    agentName: string,
    groupLabel: string,
    extra: string[],
    onFix?: () => void,
  ) {
    const chips = chipsFor(agentName, extra);
    if (chips === null) return null;
    if (chips === "error") {
      return (
        <p style={{ ...labelStyle, margin: 0 }}>
          Capabilities unavailable — this agent could not be resolved.
        </p>
      );
    }
    const fixable = chips.some((c) => c.fixable);
    return (
      <>
        <div className="workflow-step-tools" role="group" aria-label={groupLabel}>
          {chips.map((c) => (
            <span key={c.key} className="workflow-tool-chip" data-state={c.state}>
              <span>{c.label}</span>
              {/* Help lives in a tooltip; the remedy does not. Tooltip content is
                  portaled to document.body, so a control inside it is announced
                  as description text and is unreachable by keyboard. */}
              <HelpTooltip label={`About ${c.label}`} size="sm">
                {c.help}
              </HelpTooltip>
            </span>
          ))}
        </div>
        {fixable && onFix && (
          <button type="button" className="workflow-tool-fix" onClick={onFix}>
            Edit steps to switch tools on
          </button>
        )}
      </>
    );
  }

  const readyDocs = docs.filter((d) => d.status === "ready");
  const docSummary =
    selectedDocIds.length > 0 ? `${selectedDocIds.length} selected` : "all my documents";

  const runDisabled = running || busy || !runInput.trim() || !runModel || !runTarget;

  // Extracted before the JSX so TypeScript narrows the union once. `sessionId`
  // is hoisted to its own const because narrowing a property access is discarded
  // inside a callback, whereas narrowing a const survives into one.
  const result =
    runState.phase === "idle" || runState.phase === "running" ? null : runState;
  const resultSessionId = result?.sessionId ?? null;

  return (
    <div className="studio-pane">
      <div className="studio-list">
        <button onClick={startNew} disabled={busy} style={primaryBtn}>
          + New workflow
        </button>
        <ul className="studio-list-scroll">
          {mine.length === 0 && (
            <li style={{ color: "var(--fg-muted)", fontSize: "0.85em", padding: 8 }}>
              No workflows yet.
            </li>
          )}
          {mine.map((w) => (
            <li key={w.id} className="studio-list-row">
              <button
                onClick={() => startEdit(w)}
                aria-current={editing === w.name ? "true" : undefined}
                className="studio-list-select"
              >
                {w.displayName || w.name}
                {!w.enabled && <span className="studio-list-off"> (off)</span>}
              </button>
              <button
                onClick={() => remove(w.name)}
                disabled={busy}
                aria-label={`Delete ${w.name}`}
                title="Delete"
                className="studio-list-delete"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="studio-work">
        {/* Roving tabindex, and both tabs point at ONE always-present panel that
            swaps its contents — so aria-controls can never reference an id that
            is not in the DOM. */}
        <div className="studio-worktabs" role="tablist" aria-label="Workflow editor">
          {(["build", "run"] as const).map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`workflow-tab-${id}`}
              aria-selected={tab === id}
              aria-controls="workflow-panel"
              tabIndex={tab === id ? 0 : -1}
              onClick={() => setTab(id)}
              onKeyDown={(event) => {
                const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
                if (!keys.includes(event.key)) return;
                event.preventDefault();
                const next =
                  event.key === "Home" ? "build" : event.key === "End" ? "run" : id === "build" ? "run" : "build";
                setTab(next);
                document.getElementById(`workflow-tab-${next}`)?.focus();
              }}
            >
              {id === "build" ? "Build" : "Run & test"}
            </button>
          ))}
        </div>

        <div
          className="studio-workpanel"
          id="workflow-panel"
          role="tabpanel"
          aria-labelledby={`workflow-tab-${tab}`}
        >
          {error && (
            <p role="alert" className="studio-alert">
              {error}
            </p>
          )}

          {tab === "build" ? (
            <>
              <h3 style={{ margin: 0, fontSize: "1em" }}>
                {editing ? `Edit ${editing}` : "New workflow"}
              </h3>

              <div>
                <label style={labelStyle} htmlFor="wf-name">
                  Name
                </label>
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
                <label style={labelStyle} htmlFor="wf-display">
                  Display name
                </label>
                <input
                  id="wf-display"
                  value={form.displayName}
                  maxLength={MAX_DISPLAY_NAME_LEN}
                  onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
                  style={inputStyle}
                />
              </div>

              <div>
                <label style={labelStyle} htmlFor="wf-desc">
                  Description
                </label>
                <input
                  id="wf-desc"
                  value={form.description}
                  maxLength={MAX_DESCRIPTION_LEN}
                  onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                  style={inputStyle}
                />
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div
                  style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
                >
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span style={labelStyle}>
                      Steps ({form.steps.length}/{MAX_STEPS})
                    </span>
                    <HelpTooltip label="About workflow steps" size="sm">
                      Steps run in order. Each is at least one model call, but a step whose agent
                      uses tools can take up to three (an initial tool-calling attempt, a follow-up
                      with the tool result, and a forced final answer if it keeps requesting tools) —
                      every extra step and tool call adds latency and cost. Only the immediately
                      prior step&apos;s output is passed forward via {"{previous}"}, truncated to
                      8,000 characters — not the full conversation history.
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
                <div className="workflow-steps">
                  {form.steps.map((s, i) => {
                    const selectedAgent = agentsByName.get(s.agent);
                    return (
                      <div key={s.key} className="workflow-step">
                        <div className="workflow-step-head">
                          <strong className="workflow-step-index">Step {i + 1}</strong>
                          <select
                            aria-label={`Step ${i + 1} agent`}
                            value={s.agent}
                            onChange={(e) => patchStep(s.key, { agent: e.target.value })}
                            style={{ ...inputStyle, width: "100%" }}
                          >
                            {agentOptions(s.agent)}
                          </select>
                          <span className="workflow-step-actions">
                            <button onClick={() => moveStep(s.key, -1)} disabled={i === 0} aria-label={`Move step ${i + 1} up`}>
                              ↑
                            </button>
                            <button
                              onClick={() => moveStep(s.key, 1)}
                              disabled={i === form.steps.length - 1}
                              aria-label={`Move step ${i + 1} down`}
                            >
                              ↓
                            </button>
                            <button
                              onClick={() => removeStep(s.key)}
                              disabled={form.steps.length <= 1}
                              aria-label={`Remove step ${i + 1}`}
                            >
                              ✕
                            </button>
                          </span>
                        </div>
                        {selectedAgent?.description && (
                          <p style={{ ...labelStyle, margin: 0 }}>{selectedAgent.description}</p>
                        )}
                        {renderChips(s.agent, `Step ${i + 1} capabilities`, s.extraTools)}
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
                          <p
                            role="alert"
                            style={{ color: "var(--danger)", fontSize: "0.8em", margin: 0 }}
                          >
                            First step must include {INPUT_TOKEN}.
                          </p>
                        )}
                        {/* aria-label pins the group's accessible name. Without
                            it the name comes from the <legend>, which would fold
                            in the tooltip button's text and read identically for
                            every step. */}
                        <fieldset
                          className="workflow-step-toolpicker"
                          aria-label={`Step ${i + 1} tools`}
                        >
                          <legend>
                            Tools for this step
                            <HelpTooltip label="About step tools" size="sm">
                              These are granted on top of whatever the step&apos;s agent already
                              has — they never replace them. Use this when the agent you picked is
                              one of the built-in ones, whose tools you cannot edit. Only tools
                              that actually work inside a workflow step are listed here.
                            </HelpTooltip>
                          </legend>
                          {STEP_ATTACHABLE_TOOLS.map((t) => (
                            <label key={t} className="workflow-step-tool">
                              <input
                                type="checkbox"
                                checked={s.extraTools.includes(t)}
                                onChange={() => toggleStepTool(s.key, t)}
                              />
                              {TOOL_LABELS[t] ?? t}
                            </label>
                          ))}
                        </fieldset>
                      </div>
                    );
                  })}
                </div>
              </div>

              <label style={checkRow}>
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
                />
                Enabled
              </label>

              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button onClick={submit} disabled={busy} style={primaryBtn}>
                  {busy ? "Saving…" : editing ? "Save changes" : "Create workflow"}
                </button>
                {editing && (
                  <button onClick={() => setTab("run")} style={secondaryBtn}>
                    Run &amp; test →
                  </button>
                )}
              </div>
            </>
          ) : !runTarget ? (
            <p style={{ ...labelStyle, margin: 0 }}>
              Save this workflow first, or pick one from the list, then run it here.
            </p>
          ) : (
            <>
              <div className="workflow-run-target">
                <strong>{runTarget.displayName || runTarget.name}</strong>
                <span style={{ ...labelStyle, margin: 0 }}>
                  {runTarget.steps.length} step{runTarget.steps.length === 1 ? "" : "s"}
                  {runModel ? ` · ${runModel}` : ""}
                </span>
              </div>

              <div className="workflow-field">
                <div className="workflow-field-head">
                  <label htmlFor="workflow-run-input">Input</label>
                  <span id="workflow-run-input-hint">
                    {runInput.length} / {MAX_RUN_INPUT_LEN}
                  </span>
                </div>
                <textarea
                  id="workflow-run-input"
                  aria-describedby="workflow-run-input-hint"
                  placeholder="Text the first step works from…"
                  value={runInput}
                  rows={4}
                  maxLength={MAX_RUN_INPUT_LEN}
                  onChange={(e) => setRunInput(e.target.value)}
                  style={inputStyle}
                />
                <p style={{ ...labelStyle, margin: 0 }}>
                  Substituted into {INPUT_TOKEN} wherever a step uses it.
                </p>
              </div>

              {library.enabled && (
                <WorkflowDisclosure
                  id="documents"
                  title="Documents"
                  summary={docSummary}
                  expanded={openSections.has("documents")}
                  onToggle={() => toggleSection("documents")}
                >
                  {docs.length === 0 ? (
                    <p style={{ ...labelStyle, margin: 0 }}>
                      No documents in your library yet. Add one from the Library panel and it can be
                      attached to a run.
                    </p>
                  ) : (
                    <ul className="workflow-doc-list">
                      {docs.map((d, index) => {
                        const ready = d.status === "ready";
                        const atCap =
                          selectedDocIds.length >= MAX_DOCS_PER_RUN &&
                          !selectedDocIds.includes(d.id);
                        return (
                          <li key={d.id} className="workflow-doc-row">
                            <input
                              type="checkbox"
                              id={`workflow-doc-${index}`}
                              checked={selectedDocIds.includes(d.id)}
                              disabled={!ready || atCap}
                              aria-describedby={ready ? undefined : `workflow-doc-note-${index}`}
                              onChange={() => toggleDoc(d.id)}
                            />
                            <label htmlFor={`workflow-doc-${index}`}>
                              {d.filename}
                              <small>
                                {formatBytes(d.size)} · {d.modality}
                              </small>
                            </label>
                            <span className="workflow-doc-state">{DOC_STATUS_LABEL[d.status]}</span>
                            {!ready && (
                              <span id={`workflow-doc-note-${index}`} hidden>
                                Only documents that finished processing can be read by a run.
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  )}
                  <p style={{ ...labelStyle, margin: 0 }}>
                    With nothing selected, a run can read any of your {readyDocs.length} ready
                    document{readyDocs.length === 1 ? "" : "s"}. Select documents to restrict it to
                    just those. A run can use at most {MAX_DOCS_PER_RUN}.
                  </p>
                </WorkflowDisclosure>
              )}

              <WorkflowDisclosure
                id="capabilities"
                title="What this run can do"
                summary={`${runTarget.steps.length} step${runTarget.steps.length === 1 ? "" : "s"}`}
                expanded={openSections.has("capabilities")}
                onToggle={() => toggleSection("capabilities")}
              >
                {runTarget.steps.map((s, i) => (
                  <div
                    key={`${s.agent}-${i}`}
                    style={{ display: "flex", flexDirection: "column", gap: 6 }}
                  >
                    <span style={{ ...labelStyle, margin: 0 }}>
                      Step {i + 1} · {agentLabel(s.agent)}
                    </span>
                    {renderChips(
                      s.agent,
                      `Step ${i + 1} capabilities`,
                      // The SAVED step's tools — a run executes what the server
                      // stored, so reading the in-progress form here would claim
                      // a capability an unsaved edit has not granted yet.
                      s.extraTools ?? [],
                      () => setTab("build"),
                    )}
                  </div>
                ))}
              </WorkflowDisclosure>

              {durableAvailable && (
                <div className="workflow-run-option">
                  <input
                    type="checkbox"
                    id="workflow-run-durable"
                    checked={runDurable}
                    disabled={running}
                    onChange={(e) => setRunDurable(e.target.checked)}
                  />
                  <label htmlFor="workflow-run-durable">Keep running if the app restarts</label>
                  <HelpTooltip label="About durable runs" size="sm">
                    Runs the workflow on a durable orchestration, so it survives an app restart,
                    scale-in, or crash. Slower to report back, because the reply is written when the
                    run finishes rather than held open in the request.
                  </HelpTooltip>
                </div>
              )}

              <div className="workflow-run-actions">
                <button
                  onClick={() => void doRun(runTarget.name)}
                  disabled={runDisabled}
                  style={primaryBtn}
                >
                  {running ? "Running…" : "Run"}
                </button>
                <button onClick={saveAndRun} disabled={runDisabled} style={secondaryBtn}>
                  Save &amp; run
                </button>
                {!runModel && (
                  <span className="workflow-run-hint">Pick a model in the chat header first.</span>
                )}
              </div>

              {/* Separate node from the role="alert" above: a polite status and an
                  assertive error must not share a live region, or one overwrites
                  the other's announcement. */}
              <p className="workflow-run-status" role="status" aria-live="polite" aria-atomic="true">
                {running
                  ? runStatus
                    ? `Running — ${runStatus.toLowerCase()}`
                    : `Running — ${runTarget.steps.length} step${runTarget.steps.length === 1 ? "" : "s"}`
                  : ""}
              </p>

              {result && (
                <WorkflowRunReport
                  result={result}
                  pollBudgetSeconds={POLL_BUDGET_SECONDS}
                  onOpenChat={resultSessionId ? () => onRun(resultSessionId) : undefined}
                  onRunAgain={() => void doRun(runTarget.name)}
                  runAgainDisabled={runDisabled}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
