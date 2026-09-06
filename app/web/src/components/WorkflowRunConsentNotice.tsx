"use client";

import { useEffect, useEffectEvent, useRef, useState } from "react";
import * as api from "@/lib/api";
import type { Message, ToolConsentSummary, WorkflowRunStatus } from "@/lib/types";
import { ToolConsentDetails } from "./ToolConsentControls";
import { useToolConsentActive } from "./useSessionToolConsent";

export interface WorkflowRunWatch {
  sessionId: string;
  workflowName: string;
  label: string;
  runId?: string | null;
  consent?: ToolConsentSummary | null;
  mode?: "direct" | "durable";
  idempotencyKey?: string;
  startPending?: boolean;
  autoApproveRequested?: boolean;
  // UI knowledge from the cancellation/status response, not approval proof.
  cancellationAcknowledged?: boolean;
}

// A fresh run has its own session. Do not borrow a different assistant turn's
// consent if a future API ever reuses that session for multiple invocations.
export function findWorkflowRunMessage(messages: Message[], target: WorkflowRunWatch): Message | null {
  const candidates = messages.filter((message) =>
    message.role === "assistant" && message.sessionId === target.sessionId && message.workflowRunId &&
    (target.runId ? message.workflowRunId === target.runId : message.agent === `workflow:${target.workflowName}`),
  );
  return candidates.length === 1 ? candidates[0] : null;
}

interface CancelAttempt {
  active: boolean;
  pending: boolean;
  wakeRetry: (() => void) | null;
}

export function WorkflowRunConsentNotice({
  target,
  onMessage,
  onDismiss,
}: {
  target: WorkflowRunWatch;
  onMessage: (message: Message) => void;
  onDismiss: () => void;
}) {
  const [message, setMessage] = useState<Message | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [pending, setPending] = useState(false);
  const [waitingForStart, setWaitingForStart] = useState(false);
  const [cancelResult, setCancelResult] = useState<string | null>(null);
  const mutation = useRef<CancelAttempt>({ active: true, pending: false, wakeRetry: null });
  const startPendingRef = useRef(target.startPending === true);
  useEffect(() => {
    const owner: CancelAttempt = { active: true, pending: false, wakeRetry: null };
    mutation.current = owner;
    return () => { owner.active = false; owner.wakeRetry?.(); };
  }, []);
  useEffect(() => {
    startPendingRef.current = target.startPending === true;
    if (!startPendingRef.current) mutation.current.wakeRetry?.();
  }, [target.startPending]);

  // React 19.2: receive the latest callback without restarting the session poll
  // whenever the parent renders a new result or changes another workflow.
  const receiveMessage = useEffectEvent((value: Message) => {
    setMessage(value);
    onMessage(value);
  });
  const { sessionId, workflowName, runId: acceptedRunId } = target;
  const revoked = target.cancellationAcknowledged === true || cancelResult === "TERMINATED" ||
    message?.workflowConsentRevoked === true || message?.status === "cancelled";
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const messages = await api.listMessages(sessionId, controller.signal);
        if (cancelled) return;
        const observed = findWorkflowRunMessage(messages, { sessionId, workflowName, label: "", runId: acceptedRunId });
        if (observed) {
          receiveMessage(observed);
          // Cancelled is an authorization outcome, not receipt finality: the
          // backend can write that status before in-flight work appends evidence.
          if (!revoked && !observed.workflowConsentRevoked && observed.status !== "cancelled" && observed.status !== "streaming") return;
        }
        if (++attempts >= 80) {
          setError(revoked
            ? "Execution evidence monitoring paused. Cancellation remains acknowledged; refresh to check for late receipts."
            : "Live consent monitoring paused. Refresh to check the latest state; this does not stop the run.");
          return;
        }
        timer = setTimeout(() => void poll(), 1500);
      } catch (reason) {
        if (!cancelled) setError(`Unable to refresh run consent: ${reason instanceof Error ? reason.message : "request failed"}`);
      }
    };
    void poll();
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [sessionId, workflowName, acceptedRunId, revision, revoked]);

  const runId = message?.workflowRunId ?? acceptedRunId;
  const consent = message?.workflowToolConsent ?? target.consent;
  const unexpired = useToolConsentActive(consent);
  const autoApproval = target.autoApproveRequested === true || Boolean(consent);
  const directKey = target.mode === "direct" ? target.idempotencyKey : undefined;
  const finished = !revoked && (Boolean(message && message.status !== "streaming") ||
    ["COMPLETED", "FAILED"].includes(cancelResult ?? ""));

  async function revoke() {
    const owner = mutation.current;
    if ((!runId && !directKey) || !owner.active || owner.pending) return;
    owner.pending = true;
    setPending(true);
    setWaitingForStart(false);
    setError(null);
    try {
      let result: WorkflowRunStatus;
      for (;;) {
        try {
          result = directKey
            ? await api.cancelWorkflowRunByKey(workflowName, sessionId, directKey)
            : await api.cancelWorkflowRun(runId!, sessionId);
          break;
        } catch (reason) {
          if (!owner.active) return;
          const notActiveYet = reason instanceof Error && "status" in reason && reason.status === 404;
          if (!directKey || !notActiveYet || !startPendingRef.current) throw reason;
          // A pre-claim 404 is not a cancellation acknowledgement. Reuse the
          // exact handle while (and only while) this start request is pending.
          setWaitingForStart(true);
          await new Promise<void>((resolve) => {
            const done = () => { owner.wakeRetry = null; resolve(); };
            const timer = setTimeout(done, 500);
            owner.wakeRetry = () => { clearTimeout(timer); done(); };
          });
          if (!owner.active) return;
          if (!startPendingRef.current) throw reason;
        }
      }
      if (!owner.active) return;
      setCancelResult(result.status.toUpperCase());
      // Do not abort the original start request: its in-flight work still owns
      // the final receipt response, and acknowledged cancellation needs evidence.
      setRevision((value) => value + 1);
    } catch (reason) {
      if (owner.active) setError(`${autoApproval ? "Consent has not been confirmed revoked" : "Cancellation has not been confirmed"}: ${reason instanceof Error ? reason.message : "request failed"}`);
    } finally {
      owner.pending = false;
      if (owner.active) { setPending(false); setWaitingForStart(false); }
    }
  }

  return (
    <aside className="tool-consent-controls" aria-label={`${autoApproval ? "Run auto-approval" : "Workflow run"}: ${target.label}`}>
      <p className="tool-consent-state" role="status">
        {revoked ? consent ? "Run auto-approval revoked; stop requested." : "Run cancellation acknowledged; stop requested."
          : finished ? "This run has finished; its consent does not apply to another run."
            : consent ? unexpired ? "Auto-approval enabled for this run" : "Run auto-approval expired"
              : autoApproval ? "Auto-approval requested; waiting for the server's run record."
                : message ? "Workflow run in progress." : "Starting workflow; waiting for the server's run record."}
        {" "}{target.label}
      </p>
      {consent ? <ToolConsentDetails consent={consent} /> : null}
      <p className="workflow-run-hint">Revoking stops future workflow work. Already in-flight calls may finish; activity and receipts are retained. Closing this panel does not revoke consent.</p>
      {revoked && !error ? <p role="status" className="workflow-run-hint">Monitoring execution evidence after cancellation…</p> : null}
      {waitingForStart && pending ? <p role="status" className="workflow-run-hint">Waiting for this run to become active; cancellation is not confirmed yet.</p> : null}
      {!revoked && !finished ? <button type="button" disabled={(!runId && !directKey) || pending} onClick={() => void revoke()}>
        {pending ? waitingForStart ? "Waiting to stop run…" : autoApproval ? "Revoking run auto-approval…" : "Stopping run…"
          : autoApproval ? "Revoke auto-approval & stop run" : "Stop run"}
      </button> : finished || error ? <button type="button" onClick={onDismiss}>Dismiss run consent notice</button> : null}
      {error ? <p role="alert" className="inspector-error">{error}</p> : null}
      {error ? <button type="button" onClick={() => { setError(null); setRevision((value) => value + 1); }}>
        {revoked ? "Refresh execution evidence" : "Refresh run consent"}
      </button> : null}
    </aside>
  );
}
