import type { Message } from "./types";

export interface AutomationConfig {
  approvalsAvailable: boolean;
  schedulesAvailable: boolean;
  maxSchedules: number;
  maxRuntimeSeconds: number;
  hardDollarCapAvailable: false;
}

export interface AutomationLimits {
  maxApplicationDispatches: number;
  maxModelCalls: number;
  maxToolCalls: number;
  maxOutputTokens: number;
  maxRuntimeSeconds: number;
  spendMode: "no_hard_dollar_cap";
}

export interface AutomationSelection {
  name: string;
  model: string;
  documentIds: string[];
}

export interface AutomationStart {
  selection: AutomationSelection;
  input: string;
  limits: AutomationLimits;
  idempotencyKey: string;
}

export interface AutomationApproval {
  id: string;
  tool: string;
  label: string;
  purpose: string;
  risk: string;
  destination: string | null;
  expiresAt: string;
  state: string;
  argumentsDigest: string;
}

export interface AutomationRun {
  runId: string;
  sessionId: string;
  workflow: string;
  status: string;
  reason: string | null;
  revision: number;
  deadline: string | null;
  step: number;
  approval: AutomationApproval | null;
  message?: Message;
}

export interface AutomationReview extends AutomationRun {
  argumentsJson: string;
  requestId: string;
  grant: string;
  approvedDigest: string;
  effectiveDigest: string;
  spendImpact: string;
}

export interface ScheduleRule {
  frequency: "once" | "daily" | "weekly";
  timezone: string;
  localTime: string;
  localDate?: string | null;
  weekday?: number | null;
  maxOccurrences: number;
  gapPolicy: "skip";
  foldPolicy: "first";
  missedPolicy: "skip";
  overlapPolicy: "deny";
}

export interface ScheduleWrite extends AutomationStart {
  rule: ScheduleRule;
  expectedRevision?: number;
}

export interface WorkflowSchedule {
  id: string;
  revision: number;
  generation: number;
  enabled: boolean;
  workflow: string;
  workflowName: string;
  model: string;
  input: string;
  status: string;
  reason: string | null;
  rule: ScheduleRule;
  limits: AutomationLimits;
  next: { localSlot: string; dueAt: string; zoneVersion: string; zoneDigest: string } | null;
  consumed: number;
  tools: string[];
  history: { slot: string; dueAt: string; outcome: string; runId: string | null }[];
  approvedDigest: string;
  effectiveDigest: string;
}

export function automationKey(): string {
  return `${new Date().toISOString()}~${crypto.randomUUID()}`;
}

export const DEFAULT_AUTOMATION_LIMITS: AutomationLimits = {
  maxApplicationDispatches: 64, maxModelCalls: 18, maxToolCalls: 48,
  maxOutputTokens: 1024, maxRuntimeSeconds: 1800, spendMode: "no_hard_dollar_cap",
};

export const AUTOMATION_TERMINAL = new Set([
  "completed", "failed", "cancelled", "denied", "expired", "timed_out",
  "context_revoked", "policy_revoked", "outcome_unknown",
]);
