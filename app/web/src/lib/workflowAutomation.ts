import type { Message } from "./types";

export interface AutomationConfig {
  approvalsAvailable: boolean;
  schedulesAvailable: boolean;
  maxSchedules: number;
  maxRuntimeSeconds: number;
  hardDollarCapAvailable: false;
  monetaryCapAvailable?: boolean;
  monetaryCapProfile?: "stateless_text_only";
  monetaryCapUnavailableReason?: string | null;
}

export interface AutomationLimits {
  maxApplicationDispatches: number;
  maxModelCalls: number;
  maxToolCalls: number;
  maxOutputTokens: number;
  maxRuntimeSeconds: number;
  spendMode: "no_hard_dollar_cap" | "usd_app_meter";
  maxSpendMicroUsd?: number | null;
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
  allowTools?: boolean;
  allowAutomaticMemory?: boolean;
}

export interface WorkflowBudget {
  mode: "no_hard_dollar_cap" | "usd_app_meter";
  currency: "USD";
  budgetId: string | null;
  revision: number | null;
  limitMicroUsd: number | null;
  settledMicroUsd: number | null;
  heldMicroUsd: number | null;
  unknownMicroUsd: number | null;
  remainingMicroUsd: number | null;
  blocked: boolean;
}

export interface WorkflowSpendImpact {
  coverage: "bounded" | "unknown";
  amountMicroUsd: number | null;
  currency: "USD";
  basis: "local-no-metered-effects-v1" | "catalog-text-v1" | "catalog-embedding-v1" |
    "unbounded-operation" | "legacy-unquoted";
  reason: "repository-local-handler" | "catalog-token-envelope" | "meter-coverage-unknown" |
    "attempt-contract-unavailable" | "price-unavailable" | "legacy-unquoted";
  bounds: {
    amounts: { requests: number; tokens: number | null; microUsd: number | null; compute: number };
    basis: string;
    priceVersion: string | null;
    inputRate: string | null;
    outputRate: string | null;
    attemptVersion: string | null;
    maxAttempts: number | null;
  } | null;
  localContractDigest: string | null;
}

export interface WorkflowSpendQuote {
  version: "workflow-approval-spend-v1";
  quoteDigest: string;
  bindingDigest: string;
  quotedAt: string;
  expiresAt: string;
  impact: WorkflowSpendImpact;
  budget: WorkflowBudget;
}

export interface WorkflowSpendView {
  scope: "exact_tool_operation";
  status: "quoted" | "legacy_unquoted";
  impact: WorkflowSpendImpact;
  quote: WorkflowSpendQuote | null;
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
  spend?: WorkflowSpendView;
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
  budget?: WorkflowBudget;
}

export interface AutomationReview extends AutomationRun {
  argumentsJson: string;
  requestId: string;
  grant: string;
  approvedDigest: string;
  effectiveDigest: string;
  spendImpact: string;
  spendEvidence?: WorkflowSpendView;
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
  allowTools?: boolean;
  allowAutomaticMemory?: boolean;
}

export function parseWorkflowUsd(value: string): number | null {
  if (!/^\d{1,10}(?:\.\d{1,6})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const amount = BigInt(whole) * BigInt(1_000_000) + BigInt(fraction.padEnd(6, "0"));
  return amount <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(amount) : null;
}

export function workflowUsdInput(amount: number): string {
  if (!Number.isSafeInteger(amount) || amount < 0) throw new Error("Invalid workflow monetary amount.");
  const value = BigInt(amount);
  const decimals = (value % BigInt(1_000_000)).toString().padStart(6, "0").replace(/0+$/, "");
  return `${value / BigInt(1_000_000)}${decimals ? `.${decimals}` : ""}`;
}

export function formatWorkflowUsd(amount: number | null): string {
  if (amount === null) return "Unknown";
  const [whole, fraction = ""] = workflowUsdInput(amount).split(".");
  const decimal = new Intl.NumberFormat().formatToParts(1.1).find((part) => part.type === "decimal")?.value ?? ".";
  return `USD ${BigInt(whole).toLocaleString()}${decimal}${fraction.padEnd(2, "0")}`;
}

const moneyDigest = /^[0-9a-f]{64}$/;
const monetaryFields = [
  "limitMicroUsd", "settledMicroUsd", "heldMicroUsd", "unknownMicroUsd", "remainingMicroUsd",
] as const;
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function amount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function invalidMoney(): never {
  throw new Error("The server's monetary evidence is incomplete or inconsistent. Reload before approving this call.");
}

export function validateWorkflowBudget(value: unknown): asserts value is WorkflowBudget {
  if (!object(value) || value.currency !== "USD" || typeof value.blocked !== "boolean") invalidMoney();
  if (value.mode === "no_hard_dollar_cap") {
    if (value.budgetId !== null || value.revision !== null || value.blocked ||
      monetaryFields.some((field) => value[field] !== null)) invalidMoney();
    return;
  }
  if (value.mode !== "usd_app_meter" || typeof value.budgetId !== "string" ||
    !moneyDigest.test(value.budgetId) || !amount(value.revision) ||
    monetaryFields.some((field) => !amount(value[field]))) invalidMoney();
  const { limitMicroUsd, settledMicroUsd, heldMicroUsd, unknownMicroUsd, remainingMicroUsd } = value;
  if (!amount(limitMicroUsd) || !amount(settledMicroUsd) || !amount(heldMicroUsd) ||
    !amount(unknownMicroUsd) || !amount(remainingMicroUsd)) invalidMoney();
  const remaining = BigInt(limitMicroUsd) - BigInt(settledMicroUsd) - BigInt(heldMicroUsd);
  if (unknownMicroUsd > heldMicroUsd || BigInt(remainingMicroUsd) !== (remaining > 0 ? remaining : BigInt(0)) ||
    (!value.blocked && remaining < 0)) invalidMoney();
}

function validateImpact(value: unknown): asserts value is WorkflowSpendImpact {
  if (!object(value) || value.currency !== "USD") invalidMoney();
  if (value.coverage === "unknown") {
    if (value.amountMicroUsd !== null || value.bounds !== null || value.localContractDigest !== null ||
      !["unbounded-operation", "legacy-unquoted"].includes(String(value.basis)) ||
      !["meter-coverage-unknown", "attempt-contract-unavailable", "price-unavailable", "legacy-unquoted"].includes(String(value.reason))) invalidMoney();
    return;
  }
  if (value.coverage !== "bounded" || !amount(value.amountMicroUsd)) invalidMoney();
  if (value.basis === "local-no-metered-effects-v1") {
    if (value.amountMicroUsd !== 0 || value.bounds !== null || value.reason !== "repository-local-handler" ||
      typeof value.localContractDigest !== "string" || !moneyDigest.test(value.localContractDigest)) invalidMoney();
    return;
  }
  const bound = value.bounds;
  if (!object(bound) || !object(bound.amounts) || bound.maxAttempts !== 1 ||
    bound.amounts.microUsd !== value.amountMicroUsd || bound.amounts.requests !== 1 ||
    bound.amounts.compute !== 0 || !amount(bound.amounts.tokens) ||
    !["catalog-text-v1", "catalog-embedding-v1"].includes(String(value.basis)) ||
    bound.basis !== value.basis || value.reason !== "catalog-token-envelope" ||
    value.localContractDigest !== null ||
    ["priceVersion", "attemptVersion", "inputRate", "outputRate"].some((field) =>
      typeof bound[field] !== "string" || bound[field] === "")) invalidMoney();
}

export function validateWorkflowSpend(value: unknown): asserts value is WorkflowSpendView {
  if (!object(value) || value.scope !== "exact_tool_operation") invalidMoney();
  validateImpact(value.impact);
  if (value.status === "legacy_unquoted") {
    if (value.quote !== null || value.impact.basis !== "legacy-unquoted") invalidMoney();
    return;
  }
  const quote = value.quote;
  if (value.status !== "quoted" || !object(quote) || quote.version !== "workflow-approval-spend-v1" ||
    typeof quote.quoteDigest !== "string" || !moneyDigest.test(quote.quoteDigest) ||
    typeof quote.bindingDigest !== "string" || !moneyDigest.test(quote.bindingDigest) ||
    typeof quote.quotedAt !== "string" || typeof quote.expiresAt !== "string" ||
    !Number.isFinite(Date.parse(quote.quotedAt)) || !Number.isFinite(Date.parse(quote.expiresAt)) ||
    Date.parse(quote.expiresAt) <= Date.parse(quote.quotedAt)) invalidMoney();
  validateWorkflowBudget(quote.budget);
  validateImpact(quote.impact);
  if (JSON.stringify(quote.impact) !== JSON.stringify(value.impact)) invalidMoney();
}

export function validateAutomationMoney<T extends AutomationRun>(run: T): T {
  if (run.budget !== undefined) validateWorkflowBudget(run.budget);
  if (run.approval?.spend !== undefined) validateWorkflowSpend(run.approval.spend);
  return run;
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
