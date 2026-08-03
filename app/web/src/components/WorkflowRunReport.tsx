"use client";

// Presentational report for a settled workflow run.
//
// It renders in place rather than navigating: handing off to the chat view
// unmounts the builder and takes the output with it, which is why running a
// workflow used to appear to produce nothing at all. Hand-off is now an explicit
// button, so the user chooses when to leave.

import { Markdown } from "./Markdown";
import type { SettledRunState } from "./workflowRun";

export function WorkflowRunReport({
  result,
  pollBudgetSeconds,
  onOpenChat,
  onRunAgain,
  runAgainDisabled,
}: {
  result: SettledRunState;
  /** How long the UI waited before giving up on polling, for the timeout copy. */
  pollBudgetSeconds: number;
  /** Absent when the run produced no session to open. */
  onOpenChat?: () => void;
  onRunAgain: () => void;
  runAgainDisabled: boolean;
}) {
  const verdict =
    result.phase === "succeeded"
      ? `succeeded in ${Math.round(result.elapsedMs / 1000)}s`
      : result.phase === "failed"
        ? "failed"
        : "still running";

  return (
    <section className="workflow-result" aria-labelledby="workflow-result-heading">
      <div className="workflow-result-head">
        <h3 id="workflow-result-heading">Result</h3>
        <span className="workflow-result-verdict" data-phase={result.phase}>
          {verdict}
        </span>
      </div>

      {result.phase === "timedOut" && (
        <p className="workflow-result-note">
          This page stopped waiting after {pollBudgetSeconds}s. The run has not been cancelled — it
          is still going, and its reply will appear in the run&apos;s chat when it finishes.
        </p>
      )}

      <ol className="workflow-trace" aria-label="Step results">
        {result.steps.map((s) => (
          <li key={s.index} className="workflow-trace-item">
            <div className="workflow-trace-head">
              <span>
                {s.index + 1}. {s.agentLabel}
              </span>
              <span className="workflow-trace-state" data-state={s.state}>
                {s.state}
              </span>
            </div>
            {/* role="alert" here and role="status" on the running line are on
                different nodes on purpose: an assertive and a polite live region
                sharing one node overwrite each other's announcements. */}
            {s.error && (
              <p role="alert" className="studio-alert">
                {s.error}
              </p>
            )}
            {s.text && (
              <div className="workflow-trace-body">
                <Markdown content={s.text} />
              </div>
            )}
          </li>
        ))}
      </ol>

      <div className="workflow-result-actions">
        {onOpenChat && <button onClick={onOpenChat}>Open in chat</button>}
        <button onClick={onRunAgain} disabled={runAgainDisabled}>
          Run again
        </button>
      </div>
    </section>
  );
}
