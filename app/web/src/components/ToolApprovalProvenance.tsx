import type { ToolApprovalSource } from "@/lib/types";

const APPROVAL_LABELS: Record<ToolApprovalSource, string> = {
  session: "Auto-approved for this session",
  run: "Auto-approved for this run",
  invocation: "Approved for this invocation",
  not_required: "Approval not required",
  operator: "Approved by operator policy",
};

export function ToolApprovalProvenance({
  approval,
  consentId,
}: {
  approval?: ToolApprovalSource | null;
  consentId?: string | null;
}) {
  return (
    <div className="activity-row">
      <span className="activity-label">Approval</span>
      <span className="activity-detail">
        {approval && Object.hasOwn(APPROVAL_LABELS, approval)
          ? APPROVAL_LABELS[approval]
          : "Approval provenance not recorded"}
        {consentId ? <> · Consent <code>{consentId}</code></> : null}
      </span>
    </div>
  );
}
