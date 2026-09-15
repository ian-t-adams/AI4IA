"use client";

import { formatWorkflowUsd, type WorkflowBudget, type WorkflowSpendView } from "@/lib/workflowAutomation";

export function WorkflowBudgetEvidence({ budget }: { budget: WorkflowBudget }) {
  if (budget.mode === "no_hard_dollar_cap") return <p className="workflow-run-hint">This run has no monetary maximum.</p>;
  return <div>
    <dl className="workflow-automation-facts workflow-money-facts">
      <dt>Per-run app-meter limit</dt><dd>{formatWorkflowUsd(budget.limitMicroUsd)}</dd>
      <dt>Settled app-meter spend</dt><dd>{formatWorkflowUsd(budget.settledMicroUsd)}</dd>
      <dt>Charged reservations</dt><dd>{formatWorkflowUsd(budget.heldMicroUsd)}</dd>
      <dt>Unknown outcomes within reservations</dt><dd>{formatWorkflowUsd(budget.unknownMicroUsd)}</dd>
      <dt>Unreserved app-meter balance</dt><dd>{formatWorkflowUsd(budget.remainingMicroUsd)}</dd>
    </dl>
    <p className="workflow-run-hint">Budget revision {budget.revision}. Unknown or lost outcomes keep their full reservation; timeouts and stopping a run do not refund them.</p>
    {budget.blocked ? <p role="alert" className="studio-alert">The accounting contract no longer permits more work. Stopping or reloading does not remove its charges.</p> : null}
  </div>;
}

export function WorkflowSpendEvidence({ spend }: { spend: WorkflowSpendView }) {
  const quote = spend.quote;
  return <section aria-label="Exact-call spend impact">
    <h4>Spend impact of this call</h4>
    <p><strong>{formatWorkflowUsd(spend.impact.amountMicroUsd)}</strong>
      {spend.impact.coverage === "bounded" ? " maximum for this exact operation." : " additional spend. This is not a zero-cost estimate."}</p>
    <p className="workflow-run-hint">This quote covers only the stored tool operation, not the model work that may follow. Each later protected dispatch needs its own reservation in a capped run.</p>
    {spend.status === "legacy_unquoted" ? <p className="workflow-run-hint">This older approval has no immutable spend quote. Refreshing the quote does not repeat completed work.</p> : null}
    {spend.impact.basis === "local-no-metered-effects-v1" ? <p className="workflow-run-hint">The exact repository handler is local-only. A separate execution guard refuses metered effects.</p> : null}
    {spend.impact.bounds ? <dl className="workflow-automation-facts">
      <dt>Price version</dt><dd>{spend.impact.bounds.priceVersion}</dd>
      <dt>Attempt contract</dt><dd>{spend.impact.bounds.attemptVersion}</dd>
      <dt>Maximum upstream attempts</dt><dd>{spend.impact.bounds.maxAttempts}</dd>
    </dl> : null}
    {quote ? <>
      <WorkflowBudgetEvidence budget={quote.budget} />
      <details>
        <summary>Immutable spend quote</summary>
        <dl className="workflow-automation-facts">
          <dt>Recorded</dt><dd>{new Date(quote.quotedAt).toLocaleString()}</dd>
          <dt>Quote digest</dt><dd><code>{quote.quoteDigest}</code></dd>
          <dt>Exact-call binding</dt><dd><code>{quote.bindingDigest}</code></dd>
        </dl>
      </details>
    </> : null}
    <p className="workflow-run-hint">USD application-meter amounts use recorded app prices. They are not an Azure infrastructure or total bill cap; unsupported service charges remain unknown.</p>
  </section>;
}
