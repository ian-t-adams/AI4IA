"use client";

import { useEffect, useRef, useState } from "react";
import * as api from "@/lib/api";
import {
  AUTOMATION_TERMINAL, DEFAULT_AUTOMATION_LIMITS, automationKey,
  type AutomationConfig, type AutomationRun, type AutomationStart,
  type ScheduleRule, type ScheduleWrite, type WorkflowSchedule,
} from "@/lib/workflowAutomation";
import type { Workflow } from "@/lib/types";
import { ExecutionReceiptPanel } from "./ExecutionEvidence";
import { WorkflowApprovalInbox } from "./WorkflowApprovalInbox";
import { inputStyle, primaryBtn, secondaryBtn } from "./builderStyles";

export function WorkflowAutomationPanel({
  workflow, model, documentIds, onOpenChat,
}: {
  workflow: Workflow;
  model: string | null;
  documentIds: string[];
  onOpenChat: (sessionId: string) => void;
}) {
  const [config, setConfig] = useState<AutomationConfig | null>(null);
  const [input, setInput] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [runtime, setRuntime] = useState(1800);
  const [dispatches, setDispatches] = useState(64);
  const [outputTokens, setOutputTokens] = useState(1024);
  const [frequency, setFrequency] = useState<ScheduleRule["frequency"]>("daily");
  const [zone, setZone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [localTime, setLocalTime] = useState("09:00");
  const [localDate, setLocalDate] = useState("");
  const [weekday, setWeekday] = useState(0);
  const [occurrences, setOccurrences] = useState(10);
  const [schedules, setSchedules] = useState<WorkflowSchedule[] | null>(null);
  const [editingSchedule, setEditingSchedule] = useState<WorkflowSchedule | null>(null);
  const [run, setRun] = useState<AutomationRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState<"run" | "schedule" | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [monitoringPaused, setMonitoringPaused] = useState(false);
  const pendingRun = useRef<AutomationStart | null>(null);
  const pendingSchedule = useRef<ScheduleWrite | null>(null);
  const pendingScheduleId = useRef<string | undefined>(undefined);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    void (async () => {
      try {
        const available = await api.getWorkflowAutomationConfig(controller.signal);
        if (controller.signal.aborted) return;
        setConfig(available ?? null);
        if (available?.approvalsAvailable) {
          setRuntime(Math.min(1800, available.maxRuntimeSeconds));
          const result = await api.listWorkflowSchedules(controller.signal);
          if (!controller.signal.aborted) setSchedules(result.schedules);
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(api.apiErrorDetail(reason));
      }
    })();
    return () => { alive.current = false; controller.abort(); };
  }, []);

  const runId = run?.runId;
  const terminal = run ? AUTOMATION_TERMINAL.has(run.status) : false;
  useEffect(() => {
    if (!runId || terminal) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    async function poll() {
      try {
        const current = await api.getAutomationRun(runId!, controller.signal);
        if (controller.signal.aborted) return;
        setRun(current);
        if (AUTOMATION_TERMINAL.has(current.status)) return;
        if (++attempts >= 80) { setMonitoringPaused(true); return; }
        timer = setTimeout(() => void poll(), 3000);
      } catch (reason) {
        if (!controller.signal.aborted) { setError(api.apiErrorDetail(reason)); setMonitoringPaused(true); }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, terminal, refresh]);

  function request(): AutomationStart {
    if (!model) throw new Error("Select a model for this workflow.");
    return {
      selection: { name: workflow.name, model, documentIds: [...documentIds] },
      input,
      limits: {
        ...DEFAULT_AUTOMATION_LIMITS, maxRuntimeSeconds: runtime,
        maxApplicationDispatches: dispatches, maxOutputTokens: outputTokens,
      },
      idempotencyKey: automationKey(),
    };
  }

  async function start() {
    if (busy || !confirmed) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      pendingRun.current ??= request();
      const current = await api.startResumableWorkflow(pendingRun.current);
      if (!alive.current) return;
      setRun(current);
      setRetry(null); pendingRun.current = null; setMonitoringPaused(false);
      setNotice("Run recorded. Approval pauses and completed work survive closing this view.");
    } catch (reason) {
      if (alive.current) {
        setError(api.apiErrorDetail(reason));
        if (reason instanceof Error && "status" in reason && reason.status === 422) {
          pendingRun.current = null; setRetry(null);
        } else setRetry("run");
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function schedule() {
    if (busy || !confirmed) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      if (!pendingSchedule.current) {
        pendingScheduleId.current = editingSchedule?.id;
        pendingSchedule.current = {
          ...request(),
          expectedRevision: editingSchedule?.revision,
          rule: {
            frequency, timezone: zone, localTime,
            localDate: frequency === "once" ? localDate : null,
            weekday: frequency === "weekly" ? weekday : null,
            maxOccurrences: frequency === "once" ? 1 : occurrences,
            gapPolicy: "skip", foldPolicy: "first", missedPolicy: "skip", overlapPolicy: "deny",
          },
        };
      }
      const saved = await api.saveWorkflowSchedule(pendingSchedule.current, pendingScheduleId.current);
      if (!alive.current) return;
      pendingSchedule.current = null; setRetry(null);
      pendingScheduleId.current = undefined; setEditingSchedule(null);
      setSchedules((items) => [saved, ...(items ?? []).filter((item) => item.id !== saved.id)]);
      setNotice(`Schedule ${saved.status.replaceAll("_", " ")}. Its workflow version and safe tool surface are pinned.`);
    } catch (reason) {
      if (alive.current) {
        setError(api.apiErrorDetail(reason));
        if (reason instanceof Error && "status" in reason && reason.status === 422) {
          pendingSchedule.current = null; pendingScheduleId.current = undefined; setRetry(null);
        } else setRetry("schedule");
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function disable(value: WorkflowSchedule) {
    setBusy(true); setError(null);
    try {
      const updated = await api.disableWorkflowSchedule(value.id, value.revision);
      if (alive.current) setSchedules((items) => items?.map((item) => item.id === updated.id ? updated : item) ?? null);
    } catch (reason) {
      if (alive.current) setError(api.apiErrorDetail(reason));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  function edit(value: WorkflowSchedule) {
    if (busy || retry) return;
    setEditingSchedule(value); setInput(value.input); setConfirmed(false);
    setRuntime(value.limits.maxRuntimeSeconds); setDispatches(value.limits.maxApplicationDispatches);
    setOutputTokens(value.limits.maxOutputTokens); setFrequency(value.rule.frequency);
    setZone(value.rule.timezone); setLocalTime(value.rule.localTime.slice(0, 5));
    setLocalDate(value.rule.localDate ?? ""); setWeekday(value.rule.weekday ?? 0);
    setOccurrences(value.rule.maxOccurrences);
    setNotice("Editing creates a new generation using the current saved workflow and selected document scope. Review the settings and confirm bounded execution again.");
  }

  async function recover(value: WorkflowSchedule) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      const updated = await api.recoverWorkflowSchedule(value.id);
      if (alive.current) setSchedules((items) => items?.map((item) => item.id === updated.id ? updated : item) ?? null);
    } catch (reason) {
      if (alive.current) setError(api.apiErrorDetail(reason));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  if (!config?.approvalsAvailable) return null;
  const locked = busy || retry !== null;
  const valid = confirmed && Boolean(model) && Boolean(input.trim()) && runtime >= 1 &&
    runtime <= config.maxRuntimeSeconds && dispatches >= 1 && dispatches <= 128 && outputTokens >= 1;
  return <section className="workflow-automation-stack" aria-labelledby="workflow-automation-title">
    <h3 id="workflow-automation-title">Resumable runs and safe schedules</h3>
    <p className="workflow-run-hint">Run {workflow.displayName} with exact-call approvals, or schedule its safe read-only surface. This does not inherit chat or run auto-approval.</p>
    <label className="workflow-field">Automation input
      <textarea value={input} onChange={(event) => setInput(event.target.value)} maxLength={8000} rows={4} disabled={locked} style={inputStyle} />
    </label>
    <div className="workflow-automation-grid">
      <label>Maximum runtime (seconds)<input type="number" min={1} max={config.maxRuntimeSeconds} value={runtime} disabled={locked} onChange={(event) => setRuntime(Number(event.target.value))} style={inputStyle} /></label>
      <label>Maximum application dispatches<input type="number" min={1} max={128} value={dispatches} disabled={locked} onChange={(event) => setDispatches(Number(event.target.value))} style={inputStyle} /></label>
      <label>Output tokens per model call<input type="number" min={1} max={32768} value={outputTokens} disabled={locked} onChange={(event) => setOutputTokens(Number(event.target.value))} style={inputStyle} /></label>
    </div>
    <p className="workflow-run-hint">Up to 18 model calls and 48 tool calls, further limited by these bounds. Provider retries and infrastructure charges are not a hard spending cap.</p>
    <label className="workflow-run-option"><input type="checkbox" checked={confirmed} disabled={locked} onChange={(event) => setConfirmed(event.target.checked)} /> I choose bounded execution without a hard dollar cap.</label>
    {error ? <p role="alert" className="studio-alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {retry ? <p className="workflow-run-hint">The outcome is not confirmed. Retry sends the original saved request and key, not a new run or schedule.</p> : null}
    <div className="workflow-automation-actions">
      <button type="button" style={primaryBtn} disabled={busy || !valid || retry === "schedule"} onClick={() => void start()}>{retry === "run" ? "Retry original run start" : "Start resumable run"}</button>
    </div>
    {run ? <section aria-label="Resumable run" className="workflow-automation-review">
      <h4>{run.workflow}: {run.status.replaceAll("_", " ")}</h4>
      {run.reason ? <p className="workflow-run-hint">{run.reason}</p> : null}
      {monitoringPaused ? <p>Monitoring paused; the server still owns the run.</p> : null}
      <div className="workflow-automation-actions">
        <button type="button" style={secondaryBtn} onClick={() => onOpenChat(run.sessionId)}>Open run conversation</button>
        <button type="button" style={secondaryBtn} onClick={() => { setMonitoringPaused(false); setRefresh((value) => value + 1); }}>Refresh run</button>
      </div>
      {run.message?.executionReceipt ? <ExecutionReceiptPanel receipt={run.message.executionReceipt} /> : null}
      {run.status === "awaiting_approval" ? <WorkflowApprovalInbox /> : null}
    </section> : null}
    {config.schedulesAvailable ? <>
      <h4>Safe-only schedule</h4>
      {editingSchedule ? <p>Editing {editingSchedule.workflow}, generation {editingSchedule.generation}. Existing admitted runs are unchanged.</p> : null}
      <p className="workflow-run-hint">Only known, read-only, non-recursive tools are eligible. Ambient web tools and mutations are excluded; unavailable or unsafe selected tools block saving.</p>
      <div className="workflow-automation-grid">
        <label>Frequency<select value={frequency} disabled={locked} onChange={(event) => setFrequency(event.target.value as ScheduleRule["frequency"])} style={inputStyle}>
          <option value="once">Once</option><option value="daily">Daily</option><option value="weekly">Weekly</option>
        </select></label>
        <label>IANA timezone<input value={zone} disabled={locked} onChange={(event) => setZone(event.target.value)} maxLength={128} style={inputStyle} /></label>
        <label>Local time<input type="time" value={localTime} disabled={locked} onChange={(event) => setLocalTime(event.target.value)} style={inputStyle} /></label>
        {frequency === "once" ? <label>Local date<input type="date" value={localDate} disabled={locked} onChange={(event) => setLocalDate(event.target.value)} style={inputStyle} /></label> : null}
        {frequency === "weekly" ? <label>Weekday<select value={weekday} disabled={locked} onChange={(event) => setWeekday(Number(event.target.value))} style={inputStyle}>
          {["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((day, index) => <option key={day} value={index}>{day}</option>)}
        </select></label> : null}
        {frequency !== "once" ? <label>Maximum occurrences<input type="number" min={1} max={366} value={occurrences} disabled={locked} onChange={(event) => setOccurrences(Number(event.target.value))} style={inputStyle} /></label> : null}
      </div>
      <p className="workflow-run-hint">DST gaps are skipped; repeated times run once at the first occurrence. Runs more than five minutes late are skipped. No backfill or overlapping runs. Disabling affects future occurrences, not an already admitted run.</p>
      <div className="workflow-automation-actions">
        <button type="button" style={primaryBtn} disabled={busy || !valid || retry === "run" || (frequency === "once" && !localDate)} onClick={() => void schedule()}>{retry === "schedule" ? "Retry original schedule save" : editingSchedule ? "Save revised safe schedule" : "Save safe schedule"}</button>
        {editingSchedule ? <button type="button" style={secondaryBtn} disabled={locked} onClick={() => setEditingSchedule(null)}>Cancel schedule edit</button> : null}
      </div>
      {schedules === null ? <p role="status">Loading your schedules...</p> : null}
      <ul className="workflow-automation-list">{schedules?.filter((item) => item.workflowName === workflow.name).map((item) => <li key={item.id}>
        <strong>{item.workflow}: {item.status.replaceAll("_", " ")}</strong>
        <p>{item.rule.frequency} at {item.rule.localTime} ({item.rule.timezone}); {item.consumed}/{item.rule.maxOccurrences} occurrences</p>
        {item.next ? <p>Next: {item.next.localSlot} ({item.rule.timezone}) · {item.next.dueAt} UTC</p> : null}
        {item.reason ? <p className="studio-alert">{item.reason}</p> : null}
        <p className="workflow-run-hint">Safe tools: {item.tools.join(", ") || "none"}</p>
        <div className="workflow-automation-actions">
          {["pending", "acceptance_unknown"].includes(item.status) ? <button type="button" style={secondaryBtn} disabled={locked} onClick={() => void recover(item)}>Recover original schedule start</button> : null}
          <button type="button" style={secondaryBtn} disabled={locked} onClick={() => edit(item)}>Edit schedule</button>
          {item.enabled ? <button type="button" style={secondaryBtn} disabled={busy} onClick={() => void disable(item)}>Disable schedule</button> : null}
        </div>
      </li>)}</ul>
    </> : null}
  </section>;
}
