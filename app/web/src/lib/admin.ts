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
  // Rolling 24h cap on direct Code Interpreter sandbox executions. Its own axis
  // because a sandbox is billed per session, not per token.
  computeExecutionsPerDay?: number | null;
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
  entitlementKnown?: boolean;
  // Admin-only directory enrichment: present going forward once the user signs
  // in; null/absent -> the UI falls back to the short hash.
  displayName?: string | null;
  email?: string | null;
}

// Mirrors ai4ia_api.routers.admin_usage.AdminUsageOverviewResponse: every usage
// rollup for one window, produced by ONE bounded ledger scan. The dashboard used
// to fan out to seven legacy endpoints at once and each independently
// pulled up to 50,000 full ledger rows for the same window, which could consume
// most of a 1 GiB API replica (audit P1-15).
//
// Each section is the same shape its single-panel endpoint returns, so the panel
// components are unchanged. `partialSections` names any rollup that failed while
// the scan itself succeeded — that is what keeps one request from turning seven
// independently-degrading panels into an all-or-nothing failure.
export interface AdminUsageOverviewReport {
  sinceDays: number;
  fromTime: string;
  toTime: string;
  truncated: boolean;
  scannedRecords: number;
  summary: AdminUsageSummary;
  byModel: ModelUsageBucket[];
  byDay: DayUsageBucket[];
  totalUsers: number;
  userLimit: number;
  userOffset: number;
  byUser: AdminUserRow[];
  agents: AgentUsageBucket[];
  userAgents: UserAgentBucket[];
  byRegion: DimensionBucket[];
  byDataZone: DimensionBucket[];
  byProvider: DimensionBucket[];
  byDeployment: DimensionBucket[];
  byStatus: DimensionBucket[];
  partialSections: string[];
}

// Panel label for each server-reported partial section, so a degraded rollup
// reads the same in the error list as a wholly failed data source used to.
export const OVERVIEW_SECTION_LABELS: Record<string, string> = {
  summary: "usage summary",
  byModel: "model usage",
  byDay: "daily usage",
  byUser: "users",
  agents: "agents",
  userAgents: "user agents",
  distributions: "distributions",
  entitlements: "entitlements",
};

// Mirrors ai4ia_api.metrics.models.PanelStatus. "partial" means at least one
// (but not all) of the panel's metrics failed its own query -- the panel
// still carries every point (successful ones with a value, failed ones with
// `error` set instead), so the UI can show what did resolve rather than
// hiding the whole panel behind "unavailable".
export type PanelStatus = "ok" | "partial" | "unavailable";

export interface MetricPoint {
  name: string;
  label: string;
  aggregation: string;
  value?: number | null;
  unit?: string | null;
  // Set only when this specific metric's own query failed (a short, safe
  // reason -- see ai4ia_api.metrics.models.MetricPoint). None covers both a
  // resolved value and legitimate no-data-yet.
  error?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
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

export type OperationalPanelStatus =
  | "ok"
  | "partial"
  | "stale"
  | "unavailable";

export interface OperationalPanel {
  key: string;
  displayName: string;
  status: OperationalPanelStatus;
  source: string;
  generatedAt: string;
  sourceTimestamp?: string | null;
  lagSeconds?: number | null;
  reason?: string | null;
  rows: Record<string, unknown>[];
}

export interface OperationalMetricsReport {
  generatedAt: string;
  windowMinutes: number;
  diagnosticsUrl?: string | null;
  panels: OperationalPanel[];
}

// ---- web search health (diagnostics for the fail-soft web-search path) ----

export interface WebSearchFailure {
  category: string;
  detail?: string | null;
  at: string;
}

export interface WebSearchCategoryCount {
  category: string;
  count: number;
}

// Mirrors ai4ia_api.websearch.health.WebSearchHealthReport. `authMode` explains
// *why* calls fail: "managed_identity" + auth failures => the api's identity is
// not entitled to Web IQ; "unconfigured" => no key and the Entra fallback is off.
export interface WebSearchHealthReport {
  enabled: boolean;
  authMode: "api_key" | "managed_identity" | "unconfigured" | string;
  startedAt: string;
  generatedAt: string;
  totalCalls: number;
  successes: number;
  failures: number;
  lastSuccessAt?: string | null;
  lastFailureAt?: string | null;
  byCategory: WebSearchCategoryCount[];
  recent: WebSearchFailure[];
}

// ---- API client ----

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const resp = await apiFetch(path, { cache: "no-store", signal });
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

// One request for every usage panel. The legacy per-panel fetchers were removed
// so a future caller cannot accidentally restore seven full ledger scans.
export function fetchOverview(
  days: number,
  limit = 20,
  offset = 0,
  identify = false,
  signal?: AbortSignal,
): Promise<AdminUsageOverviewReport> {
  return getJson<AdminUsageOverviewReport>(
    `/api/admin/usage/overview?days=${days}&limit=${limit}&offset=${offset}&identify=${identify ? "true" : "false"}`,
    signal,
  );
}

export function fetchResources(signal?: AbortSignal): Promise<ResourceMetricsReport> {
  return getJson<ResourceMetricsReport>("/api/admin/metrics/resources", signal);
}

export function fetchWebSearchHealth(signal?: AbortSignal): Promise<WebSearchHealthReport> {
  return getJson<WebSearchHealthReport>("/api/admin/metrics/web-search", signal);
}

export function fetchOperations(minutes = 60, signal?: AbortSignal): Promise<OperationalMetricsReport> {
  return getJson<OperationalMetricsReport>(
    `/api/admin/metrics/operations?minutes=${minutes}`,
    signal,
  );
}

export function fetchSecurityMetrics(
  minutes = 60,
  signal?: AbortSignal,
): Promise<OperationalMetricsReport> {
  return getJson<OperationalMetricsReport>(
    `/api/admin/metrics/security?minutes=${minutes}`,
    signal,
  );
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

// Human label for a web-search failure category (falls back to the raw token).
const WEB_SEARCH_CATEGORY_LABELS: Record<string, string> = {
  config: "Not configured",
  credential: "Credential",
  auth: "Auth",
  permission: "Permission",
  rate_limit: "Rate limit",
  timeout: "Timeout",
  connection: "Connection",
  bad_request: "Bad request",
  not_found: "Not found",
  server_error: "Server error",
  status: "Upstream status",
  unknown: "Unknown",
};

export function webSearchCategoryLabel(category: string): string {
  return WEB_SEARCH_CATEGORY_LABELS[category] ?? category;
}

export interface WebSearchHint {
  tone: "ok" | "info" | "warn";
  text: string;
}

// Turn the raw health posture into a single operator-facing diagnosis. This is
// the panel's headline: it converts (enabled, authMode, recent failure
// categories) into the most likely root cause AND the concrete fix, so an admin
// doesn't have to reverse-engineer it from the counters. The common production
// bug — feature on, no key, so the api falls back to a managed identity that is
// not entitled to Web IQ — is called out explicitly.
export function webSearchHint(r: WebSearchHealthReport | null | undefined): WebSearchHint {
  if (!r) return { tone: "info", text: "Web search health is unavailable." };
  if (!r.enabled) {
    return {
      tone: "info",
      text:
        "Web search is disabled (AI4IA_WEB_SEARCH_ENABLED is off). No web tools are advertised to the model.",
    };
  }
  const count = (cat: string): number =>
    r.byCategory.filter((c) => c.category === cat).reduce((n, c) => n + c.count, 0);
  const authish = count("auth") + count("permission");
  if (r.authMode === "unconfigured") {
    return {
      tone: "warn",
      text:
        "Web search is enabled but no credentials are configured — set AI4IA_WEBIQ_API_KEY, or enable the managed-identity fallback (AI4IA_WEBIQ_USE_ENTRA).",
    };
  }
  // A managed-identity token could not be ACQUIRED at all — a different failure
  // (and a different fix) than a token that was acquired but rejected as
  // unentitled (the `auth` case below).
  if (count("credential") > 0 && r.authMode === "managed_identity") {
    return {
      tone: "warn",
      text:
        "Web search is set to use the app's managed identity but a token could not be acquired at all — the Container App likely has no usable managed identity, cannot reach the token endpoint (IMDS), or is requesting the wrong scope. Fix the managed-identity assignment; this is separate from a Web IQ entitlement problem.",
    };
  }
  if (authish > 0 && r.authMode === "managed_identity") {
    return {
      tone: "warn",
      text:
        "Web search authenticates as the app's managed identity but calls are failing authorization — the managed identity is almost certainly not entitled to Web IQ. Grant it access or set AI4IA_WEBIQ_API_KEY.",
    };
  }
  if (authish > 0 && r.authMode === "api_key") {
    return {
      tone: "warn",
      text:
        "Web search is using an API key but calls are failing authorization — the key may be invalid, expired, or lacking Web IQ entitlement.",
    };
  }
  // Reachable-but-failing patterns: an upstream incident (5xx) reads differently
  // from the service being slow (timeout), so surface whichever dominates.
  const serverish = count("server_error");
  const timeoutish = count("timeout");
  if (serverish > 0 && serverish >= timeoutish) {
    return {
      tone: "warn",
      text:
        "Web search is reaching Web IQ but it is returning server errors (HTTP 5xx) — most likely an upstream incident. Retry later; this is not a configuration problem on our side.",
    };
  }
  if (timeoutish > 0) {
    return {
      tone: "warn",
      text:
        "Web search calls are timing out — Web IQ may be slow or overloaded, or the client timeout budget is too low. Retry; if it persists, check latency and capacity.",
    };
  }
  if (r.failures > 0) {
    return {
      tone: "warn",
      text: "Web search is enabled but some recent calls failed — see the categories below.",
    };
  }
  if (r.totalCalls === 0) {
    return {
      tone: "ok",
      text: "Web search is enabled and configured. No calls recorded yet on this replica.",
    };
  }
  return { tone: "ok", text: "Web search is healthy on this replica." };
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
export function entitlementLabel(
  ent: EntitlementView | null | undefined,
  known = true,
): string {
  if (!known) return "Unavailable";
  if (!ent) return "Unlimited";
  if (ent.disabled) return "Disabled";
  if (ent.isUnlimited) return "Unlimited";
  const parts: string[] = [];
  if (ent.requestsPerMinute != null) parts.push(`${ent.requestsPerMinute}/min`);
  if (ent.tokensPerDay != null) parts.push(`${formatCompact(ent.tokensPerDay)} tok/day`);
  if (ent.tokensPerMonth != null) parts.push(`${formatCompact(ent.tokensPerMonth)} tok/mo`);
  if (ent.costPerDayMicroUsd != null) parts.push(`${formatUsd(ent.costPerDayMicroUsd)}/day`);
  if (ent.costPerMonthMicroUsd != null) parts.push(`${formatUsd(ent.costPerMonthMicroUsd)}/mo`);
  if (ent.computeExecutionsPerDay != null)
    parts.push(`${formatCompact(ent.computeExecutionsPerDay)} runs/day`);
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
