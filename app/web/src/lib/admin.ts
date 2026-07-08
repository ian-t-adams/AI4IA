// Admin dashboard client. Types mirror the FastAPI admin usage/metrics
// responses; the API helpers go through `apiFetch` (same-origin proxy, Entra
// bearer when enabled). The pure transforms/formatters at the bottom carry all
// the display logic so they can be unit-tested without a DOM (see admin.test.ts).
import { apiFetch } from "./auth";

// ---- response types (mirror app/api routers/admin_usage.py) ----

export interface WhoAmI {
  subject: string;
  isAdmin: boolean;
  email?: string | null;
  name?: string | null;
}

export interface AdminUsageSummary {
  sinceDays: number;
  fromTime: string;
  toTime: string;
  truncated: boolean;
  scannedRecords: number;
  activeUsers: number;
  totalRequests: number;
  billableRequests: number;
  unknownUsageRequests: number;
  cancelledRequests: number;
  erroredRequests: number;
  errorRate: number;
  totalPromptTokens: number;
  totalCompletionTokens: number;
  totalTokens: number;
  totalCostMicroUsd: number;
  costUnknownRequests: number;
  currency: string;
  distinctModels: number;
  distinctAgents: number;
}

export interface ModelUsageBucket {
  model: string;
  requests: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  costMicroUsd: number;
  costKnown: boolean;
}

export interface DayUsageBucket {
  day: string;
  requests: number;
  totalTokens: number;
  costMicroUsd: number;
}

export interface AgentUsageBucket {
  agent: string;
  requests: number;
  erroredRequests: number;
  cancelledRequests: number;
  totalTokens: number;
  costMicroUsd: number;
  users: number;
}

export interface UserAgentBucket {
  userId: string;
  agent: string;
  requests: number;
  totalTokens: number;
  erroredRequests: number;
  // Admin-only directory enrichment: present going forward once the user signs
  // in; null/absent -> the UI falls back to the short hash.
  displayName?: string | null;
  email?: string | null;
}

export interface DimensionBucket {
  key: string;
  requests: number;
  erroredRequests: number;
  totalTokens: number;
  costMicroUsd: number;
  costKnown: boolean;
}

export interface EntitlementView {
  userId: string;
  source: string;
  isUnlimited: boolean;
  disabled: boolean;
  requestsPerMinute?: number | null;
  tokensPerDay?: number | null;
  costPerDayMicroUsd?: number | null;
  tokensPerMonth?: number | null;
  costPerMonthMicroUsd?: number | null;
  note?: string | null;
  updatedAt?: string | null;
  updatedBy?: string | null;
}

export interface AdminUserRow {
  userId: string;
  requests: number;
  erroredRequests: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  costMicroUsd: number;
  costKnown: boolean;
  lastActiveAt?: string | null;
  entitlement?: EntitlementView | null;
  // Admin-only directory enrichment: present going forward once the user signs
  // in; null/absent -> the UI falls back to the short hash.
  displayName?: string | null;
  email?: string | null;
}

export interface AdminByModelReport {
  sinceDays: number;
  truncated: boolean;
  scannedRecords: number;
  byModel: ModelUsageBucket[];
}

export interface AdminByDayReport {
  sinceDays: number;
  truncated: boolean;
  scannedRecords: number;
  byDay: DayUsageBucket[];
}

export interface AdminAgentsReport {
  sinceDays: number;
  truncated: boolean;
  scannedRecords: number;
  agents: AgentUsageBucket[];
}

export interface AdminUserAgentsReport {
  sinceDays: number;
  truncated: boolean;
  scannedRecords: number;
  userAgents: UserAgentBucket[];
}

export interface AdminDistributionsReport {
  sinceDays: number;
  truncated: boolean;
  scannedRecords: number;
  byRegion: DimensionBucket[];
  byDataZone: DimensionBucket[];
  byDeployment: DimensionBucket[];
  byStatus: DimensionBucket[];
}

export interface AdminByUserResponse {
  sinceDays: number;
  fromTime: string;
  toTime: string;
  truncated: boolean;
  scannedRecords: number;
  totalUsers: number;
  limit: number;
  offset: number;
  byUser: AdminUserRow[];
}

export type PanelStatus = "ok" | "unavailable";

export interface MetricPoint {
  name: string;
  label: string;
  aggregation: string;
  value?: number | null;
  unit?: string | null;
}

export interface ResourcePanel {
  key: string;
  displayName: string;
  status: PanelStatus;
  detail?: string | null;
  metrics: MetricPoint[];
}

export interface ResourceMetricsReport {
  generatedAt: string;
  windowMinutes: number;
  panels: ResourcePanel[];
}

// ---- API client ----

async function getJson<T>(path: string): Promise<T> {
  const resp = await apiFetch(path, { cache: "no-store" });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body?.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    const err = new Error(`${resp.status}: ${detail}`) as Error & { status?: number };
    err.status = resp.status;
    throw err;
  }
  return (await resp.json()) as T;
}

export function fetchWhoAmI(): Promise<WhoAmI> {
  return getJson<WhoAmI>("/api/admin/whoami");
}

export function fetchSummary(days: number): Promise<AdminUsageSummary> {
  return getJson<AdminUsageSummary>(`/api/admin/usage/summary?days=${days}`);
}

export function fetchByModel(days: number): Promise<AdminByModelReport> {
  return getJson<AdminByModelReport>(`/api/admin/usage/by-model?days=${days}`);
}

export function fetchByDay(days: number): Promise<AdminByDayReport> {
  return getJson<AdminByDayReport>(`/api/admin/usage/by-day?days=${days}`);
}

export function fetchAgents(days: number): Promise<AdminAgentsReport> {
  return getJson<AdminAgentsReport>(`/api/admin/usage/agents?days=${days}`);
}

export function fetchUserAgents(days: number, identify = false): Promise<AdminUserAgentsReport> {
  return getJson<AdminUserAgentsReport>(
    `/api/admin/usage/user-agents?days=${days}&identify=${identify ? "true" : "false"}`,
  );
}

export function fetchDistributions(days: number): Promise<AdminDistributionsReport> {
  return getJson<AdminDistributionsReport>(`/api/admin/usage/distributions?days=${days}`);
}

export function fetchByUser(
  days: number,
  limit = 20,
  offset = 0,
  identify = false,
): Promise<AdminByUserResponse> {
  return getJson<AdminByUserResponse>(
    `/api/admin/usage/by-user?days=${days}&limit=${limit}&offset=${offset}&identify=${identify ? "true" : "false"}`,
  );
}

export function fetchResources(): Promise<ResourceMetricsReport> {
  return getJson<ResourceMetricsReport>("/api/admin/metrics/resources");
}

// ---- pure transforms / formatters (unit-tested) ----

// The cosmetic gate for showing the admin nav entry / dashboard body. The server
// `require_admin` is the real boundary; this only decides UI visibility.
export function canShowAdmin(whoami: WhoAmI | null | undefined): boolean {
  return !!whoami && whoami.isAdmin === true;
}

// Compact human counts: 950 -> "950", 1234 -> "1.2K", 3_400_000 -> "3.4M".
export function formatCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "0";
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  const units = [
    { v: 1e9, s: "B" },
    { v: 1e6, s: "M" },
    { v: 1e3, s: "K" },
  ];
  for (const u of units) {
    if (abs >= u.v) {
      const scaled = n / u.v;
      const str = scaled.toFixed(Math.abs(scaled) >= 100 ? 0 : 1);
      return str.replace(/\.0$/, "") + u.s;
    }
  }
  return String(Math.round(n));
}

export const formatTokens = formatCompact;

export function microUsdToUsd(micro: number | null | undefined): number {
  if (micro == null || !Number.isFinite(micro)) return 0;
  return micro / 1_000_000;
}

// Micro-USD -> "$x.xx" (4 decimals for sub-dollar amounts so cents-of-a-cent
// costs aren't rounded to $0.00).
export function formatUsd(micro: number | null | undefined): string {
  const usd = microUsdToUsd(micro);
  const abs = Math.abs(usd);
  const digits = abs > 0 && abs < 1 ? 4 : 2;
  return (
    "$" +
    usd.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })
  );
}

// Rate in [0,1] -> "12.3%" (trailing .0 trimmed).
export function formatPercent(rate: number | null | undefined, digits = 1): string {
  if (rate == null || !Number.isFinite(rate)) return "0%";
  return (rate * 100).toFixed(digits).replace(/\.0$/, "") + "%";
}

// Scale a value to a pixel length within [0, maxPx], guarding div-by-zero.
export function barScale(value: number, max: number, maxPx: number): number {
  if (max <= 0 || !Number.isFinite(value) || value <= 0) return 0;
  return Math.max(0, Math.min(maxPx, (value / max) * maxPx));
}

function round(n: number): number {
  return Math.round(n * 100) / 100;
}

// Build an SVG polyline `points` string for a sparkline: x spans [0,width],
// y is inverted (0 at top) and normalized to the series max.
export function linePoints(
  values: number[],
  width: number,
  height: number,
): string {
  if (!values.length) return "";
  const max = Math.max(...values, 0);
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  return values
    .map((v, i) => {
      const x = i * stepX;
      const y = max > 0 ? height - (v / max) * height : height;
      return `${round(x)},${round(y)}`;
    })
    .join(" ");
}

// Short, human label for a user's entitlement in the top-users table.
export function entitlementLabel(ent: EntitlementView | null | undefined): string {
  if (!ent) return "Unlimited";
  if (ent.disabled) return "Disabled";
  if (ent.isUnlimited) return "Unlimited";
  const parts: string[] = [];
  if (ent.requestsPerMinute != null) parts.push(`${ent.requestsPerMinute}/min`);
  if (ent.tokensPerDay != null) parts.push(`${formatCompact(ent.tokensPerDay)} tok/day`);
  if (ent.tokensPerMonth != null) parts.push(`${formatCompact(ent.tokensPerMonth)} tok/mo`);
  if (ent.costPerDayMicroUsd != null) parts.push(`${formatUsd(ent.costPerDayMicroUsd)}/day`);
  if (ent.costPerMonthMicroUsd != null) parts.push(`${formatUsd(ent.costPerMonthMicroUsd)}/mo`);
  return parts.length ? parts.join(", ") : "Limited";
}

// Short, copy-pasteable internal user id for display (keeps the full id for the
// title attribute, but readable in the table).
export function shortUserId(userId: string): string {
  if (userId.length <= 12) return userId;
  return `${userId.slice(0, 8)}…${userId.slice(-4)}`;
}

// The label shown for a user: their captured display name when known, otherwise
// the short hash. Pure so it can be unit-tested and reused by both admin tables;
// the full hash stays available for a title/tooltip at the call site.
export function userLabel(
  displayName: string | null | undefined,
  userId: string,
): string {
  const name = displayName?.trim();
  return name ? name : shortUserId(userId);
}

// "" when there are no errors (so callers can treat it as falsy and skip the
// danger styling), else a compact "N error(s)" label.
export function errorLabel(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return "";
  return `${formatCompact(n)} ${n === 1 ? "error" : "errors"}`;
}

export interface UserAgentGroup {
  userId: string;
  rows: UserAgentBucket[];
  totalRequests: number;
  totalTokens: number;
  erroredRequests: number;
  // Directory enrichment carried up from the user's rows (same per user); null
  // when unknown -> the UI shows the short hash.
  displayName?: string | null;
  email?: string | null;
}

// Collapse the flat user×agent cross-tab into per-user groups for a compact
// grouped table. Users are ordered by total tokens desc (request volume as a
// tiebreak); each user's agent rows are ordered the same way.
export function groupUserAgents(rows: UserAgentBucket[]): UserAgentGroup[] {
  const byUser = new Map<string, UserAgentGroup>();
  for (const row of rows) {
    let group = byUser.get(row.userId);
    if (!group) {
      group = {
        userId: row.userId,
        rows: [],
        totalRequests: 0,
        totalTokens: 0,
        erroredRequests: 0,
        displayName: row.displayName ?? null,
        email: row.email ?? null,
      };
      byUser.set(row.userId, group);
    }
    // Backfill name/email from whichever row first carries them (same per user).
    if (!group.displayName && row.displayName) group.displayName = row.displayName;
    if (!group.email && row.email) group.email = row.email;
    group.rows.push(row);
    group.totalRequests += row.requests;
    group.totalTokens += row.totalTokens;
    group.erroredRequests += row.erroredRequests;
  }
  const groups = [...byUser.values()];
  for (const group of groups) {
    group.rows.sort((a, b) => b.totalTokens - a.totalTokens || b.requests - a.requests);
  }
  groups.sort((a, b) => b.totalTokens - a.totalTokens || b.totalRequests - a.totalRequests);
  return groups;
}

// Human label for a raw record status key (the API emits the raw enum values).
export function statusLabel(key: string): string {
  switch (key) {
    case "complete":
      return "Completed";
    case "cancelled":
      return "Cancelled";
    case "error":
      return "Errored";
    default:
      return key;
  }
}

// Sum the request counts of a set of dimension buckets (panel total / bar max).
export function sumRequests(buckets: { requests: number }[]): number {
  return buckets.reduce((acc, b) => acc + (b.requests || 0), 0);
}

// A bucket's share of the dimension total, as a rate in [0,1] for formatPercent.
export function dimensionShare(value: number, total: number): number {
  if (!total || total <= 0 || !Number.isFinite(value) || value <= 0) return 0;
  return value / total;
}
