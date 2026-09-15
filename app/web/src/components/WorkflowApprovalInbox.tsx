"use client";

import { useEffect, useRef, useState } from "react";
import * as api from "@/lib/api";
import type { AutomationReview, AutomationRun } from "@/lib/workflowAutomation";
import { DialogFrame } from "./DialogFrame";
import { primaryBtn, secondaryBtn } from "./builderStyles";
import { WorkflowSpendEvidence } from "./WorkflowSpendEvidence";

export function WorkflowApprovalInboxEntry({ disabled = false }: { disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let loading = false;
    async function refresh() {
      if (loading || controller.signal.aborted) return;
      loading = true;
      try {
        const config = await api.getWorkflowAutomationConfig(controller.signal);
        if (controller.signal.aborted) return;
        if (config?.approvalsAvailable) {
          const result = await api.listWorkflowApprovals(controller.signal);
          if (!controller.signal.aborted) setCount(result.runs.filter((run) =>
            run.approval?.state === "pending" || ["preparation_unavailable", "outcome_unknown", "accounting_pending"].includes(run.status),
          ).length);
        } else setCount(null);
      } catch {
        if (!controller.signal.aborted) setCount(null);
      } finally {
        loading = false;
        if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 30_000);
      }
    }
    const focused = () => { clearTimeout(timer); void refresh(); };
    void refresh();
    window.addEventListener("focus", focused);
    return () => { controller.abort(); clearTimeout(timer); window.removeEventListener("focus", focused); };
  }, []);
  return <>
    <button type="button" className="sidebar-utility-action" disabled={disabled} onClick={() => setOpen(true)}
      style={{ width: "100%", padding: "8px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "transparent", color: "var(--sidebar-fg)" }}>
      Workflow approvals{count != null && count > 0 ? ` (${count})` : ""}
    </button>
    {open ? <DialogFrame ariaLabel="Workflow approvals" onClose={() => setOpen(false)} overlayPadding={12}>
      <section className="workflow-automation-dialog" onClick={(event) => event.stopPropagation()}>
        <header className="workflow-automation-heading">
          <h2>Workflow approvals</h2>
          <button type="button" style={secondaryBtn} onClick={() => setOpen(false)}>Close</button>
        </header>
        <WorkflowApprovalInbox />
      </section>
    </DialogFrame> : null}
  </>;
}

export function WorkflowApprovalInbox() {
  const [runs, setRuns] = useState<AutomationRun[] | null>(null);
  const [review, setReview] = useState<AutomationReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now);
  const mounted = useRef(true);
  const intent = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; intent.current += 1; };
  }, []);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const result = await api.listWorkflowApprovals(controller.signal);
        if (controller.signal.aborted) return;
        setRuns(result.runs);
        setError(null);
        setReview((current) => {
          if (!current) return null;
          const live = result.runs.find((item) => item.runId === current.runId);
          return live?.approval && live.approval.id === current.approval?.id &&
            live.approval.state === "pending" && live.revision === current.revision ? current : null;
        });
        timer = setTimeout(() => void load(), 15_000);
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(api.apiErrorDetail(reason));
          setReview(null);
        }
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision]);

  async function inspect(run: AutomationRun, refreshSpend = false) {
    if (!run.approval || busy) return;
    const request = ++intent.current;
    setBusy(true); setError(null); setReview(null); setNotice(null);
    try {
      const result = await api.reviewWorkflowApproval(run.runId, run.approval.id, refreshSpend);
      if (mounted.current && request === intent.current) setReview(result);
    } catch (reason) {
      if (mounted.current && request === intent.current) setError(api.apiErrorDetail(reason));
    } finally {
      if (mounted.current && request === intent.current) setBusy(false);
    }
  }

  async function decide(run: AutomationRun, decision: "approve" | "deny") {
    if (!run.approval || busy) return;
    if (decision === "approve" && (!review || review.runId !== run.runId ||
      review.approval?.id !== run.approval.id || Date.parse(run.approval.expiresAt) <= Date.now())) return;
    const request = ++intent.current;
    setBusy(true); setError(null);
    try {
      await api.decideWorkflowApproval(run.runId, run.approval.id, decision,
        decision === "approve" && review ? { requestId: review.requestId, grant: review.grant } : undefined);
      if (!mounted.current || request !== intent.current) return;
      setReview(null);
      setNotice(decision === "approve"
        ? "Decision recorded. The same run resumes after its current authorization checks."
        : "Denial recorded. No further work is authorized by this run.");
      setRevision((value) => value + 1);
    } catch (reason) {
      if (mounted.current && request === intent.current) {
        setReview(null);
        setError(`${api.apiErrorDetail(reason)} Refresh the run before trying another action; the decision may already be recorded.`);
      }
    } finally {
      if (mounted.current && request === intent.current) setBusy(false);
    }
  }

  async function cancel(run: AutomationRun) {
    setBusy(true); setError(null);
    try {
      await api.cancelAutomationRun(run.runId);
      if (mounted.current) { setReview(null); setRevision((value) => value + 1); }
    } catch (reason) {
      if (mounted.current) setError(api.apiErrorDetail(reason));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function recover(run: AutomationRun) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      await api.recoverAutomationRun(run.runId);
      if (mounted.current) setRevision((value) => value + 1);
    } catch (reason) {
      if (mounted.current) setError(api.apiErrorDetail(reason));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const expires = review?.approval ? Date.parse(review.approval.expiresAt) : Number.NaN;
  const expired = !Number.isFinite(expires) || expires <= now;
  return <div className="workflow-automation-stack">
    <p className="workflow-run-hint">These requests are stored with your runs, not this browser tab. Closing this view does not approve or cancel anything.</p>
    {notice ? <p role="status">{notice}</p> : null}
    {error ? <p role="alert" className="studio-alert">{error}</p> : null}
    <button type="button" style={secondaryBtn} disabled={busy} onClick={() => { intent.current += 1; setReview(null); setRevision((value) => value + 1); }}>Refresh pending runs</button>
    {runs === null && !error ? <p role="status">Loading pending runs...</p> : null}
    {runs?.length === 0 && !error ? <p>No workflow decisions are pending.</p> : null}
    <ul className="workflow-automation-list">
      {runs?.map((run) => <li key={run.runId}>
        <strong>{run.workflow}</strong>
        <p className="workflow-run-hint">Step {run.step + 1}: {run.status.replaceAll("_", " ")}{run.reason ? ` (${run.reason})` : ""}</p>
        {run.approval ? <p>{run.approval.label} · {run.approval.risk}{run.approval.destination ? ` · ${run.approval.destination}` : ""}</p> : null}
        <div className="workflow-automation-actions">
          {["pending", "acceptance_unknown"].includes(run.status) ? <button type="button" style={secondaryBtn} disabled={busy} onClick={() => void recover(run)}>Recover original run start</button> : null}
          {run.approval?.state === "pending" ? <>
            <button type="button" style={primaryBtn} disabled={busy} onClick={() => void inspect(run)}>Review exact call</button>
            <button type="button" style={secondaryBtn} disabled={busy} onClick={() => void inspect(run, true)}>Refresh spend quote</button>
            <button type="button" style={secondaryBtn} disabled={busy} onClick={() => void decide(run, "deny")}>Deny and stop</button>
          </> : null}
          <button type="button" style={secondaryBtn} disabled={busy} onClick={() => void cancel(run)}>Stop run</button>
        </div>
      </li>)}
    </ul>
    {review?.approval ? <section aria-label={`Exact call: ${review.approval.label}`} className="workflow-automation-review">
      <h3>Approve this exact call</h3>
      <p><strong>{review.approval.label}</strong> · {review.approval.risk}</p>
      <p>{review.approval.purpose}</p>
      <dl className="workflow-automation-facts">
        <dt>Destination</dt><dd>{review.approval.destination ?? "No external destination"}</dd>
        <dt>Expires</dt><dd>{new Date(review.approval.expiresAt).toLocaleString()}</dd>
        <dt>Effective version</dt><dd><code>{review.effectiveDigest}</code></dd>
      </dl>
      <p className="workflow-run-hint">The complete argument JSON is shown below. Approval resumes this stored call; it does not ask a model to recreate it.</p>
      <pre className="workflow-automation-json">{review.argumentsJson}</pre>
      {review.spendEvidence ? <WorkflowSpendEvidence spend={review.spendEvidence} />
        : <p className="workflow-run-hint">{review.spendImpact}</p>}
      {expired ? <p role="alert" className="studio-alert">This approval expired. It cannot be used to resume the run.</p> : null}
      <div className="workflow-automation-actions">
        <button type="button" style={primaryBtn} disabled={busy || expired} onClick={() => void decide(review, "approve")}>Approve this call and resume</button>
        <button type="button" style={secondaryBtn} disabled={busy} onClick={() => void decide(review, "deny")}>Deny and stop</button>
      </div>
    </section> : null}
  </div>;
}
