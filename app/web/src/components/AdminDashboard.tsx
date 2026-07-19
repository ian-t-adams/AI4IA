"use client";

// Admin usage dashboard. Client component that:
//  1. Confirms the viewer is an admin via /api/admin/whoami (cosmetic — the API
//     still enforces require_admin, so a non-admin only ever sees the forbidden
//     view and empty 403s).
//  2. Loads org-level rollups (summary / by-model / by-day / top-users / agents)
//     and best-effort resource panels for a selectable window, each independently
//     (Promise.allSettled) so one failing panel never blanks the page.
// All display logic lives in pure helpers in lib/admin.ts (unit-tested); this file
// is presentation only. Charts are inline SVG (no charting dependency).
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { HelpTooltip } from "./HelpTooltip";
import {
  type AdminUsageSummary,
  type AdminUserRow,
  type AgentUsageBucket,
  type DayUsageBucket,
  type DimensionBucket,
  type ModelUsageBucket,
  type OperationalMetricsReport,
  type OperationalPanel,
  type ResourcePanel,
  type UserAgentBucket,
  type WebSearchHealthReport,
  barScale,
  canShowAdmin,
  dimensionShare,
  entitlementLabel,
  errorLabel,
  fetchAgents,
  fetchByDay,
  fetchByModel,
  fetchByUser,
  fetchDistributions,
  fetchResources,
  fetchOperations,
  fetchSecurityMetrics,
  fetchSummary,
  fetchUserAgents,
  fetchWebSearchHealth,
  fetchWhoAmI,
  formatCompact,
  formatPercent,
  formatTokens,
  formatUsd,
  groupUserAgents,
  linePoints,
  shortUserId,
  statusLabel,
  sumRequests,
  userLabel,
  webSearchCategoryLabel,
  webSearchHint,
} from "@/lib/admin";

const WINDOWS = [7, 30, 90];
const IDENTITY_STORAGE_KEY = "ai4ia.admin.showRealIdentities";

const card: React.CSSProperties = {
  background: "var(--bg-elevated)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  padding: 16,
};

const sectionTitle: React.CSSProperties = {
  fontSize: "0.95em",
  fontWeight: 600,
  margin: "0 0 12px",
  color: "var(--fg)",
};

const muted: React.CSSProperties = { color: "var(--fg-muted)", fontSize: "0.8em" };

interface DashboardData {
  summary: AdminUsageSummary | null;
  byModel: ModelUsageBucket[];
  byDay: DayUsageBucket[];
  byUser: AdminUserRow[];
  agents: AgentUsageBucket[];
  userAgents: UserAgentBucket[];
  byRegion: DimensionBucket[];
  byDataZone: DimensionBucket[];
  byDeployment: DimensionBucket[];
  byStatus: DimensionBucket[];
  resources: ResourcePanel[];
  webSearch: WebSearchHealthReport | null;
  operations: OperationalMetricsReport | null;
  security: OperationalMetricsReport | null;
  loadErrors: string[];
  truncated: boolean;
}

const EMPTY: DashboardData = {
  summary: null,
  byModel: [],
  byDay: [],
  byUser: [],
  agents: [],
  userAgents: [],
  byRegion: [],
  byDataZone: [],
  byDeployment: [],
  byStatus: [],
  resources: [],
  webSearch: null,
  operations: null,
  security: null,
  loadErrors: [],
  truncated: false,
};

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div style={card}>
      <div style={muted}>{label}</div>
      <div style={{ fontSize: "1.6em", fontWeight: 700, marginTop: 4 }}>{value}</div>
      {sub ? <div style={{ ...muted, marginTop: 2 }}>{sub}</div> : null}
    </div>
  );
}

function OperationalPanels({
  report,
  emptyLabel,
}: {
  report: OperationalMetricsReport | null;
  emptyLabel: string;
}) {
  if (!report) return <div style={muted}>{emptyLabel}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {report.panels.map((panel: OperationalPanel) => {
        const columns = Array.from(
          new Set(panel.rows.flatMap((row) => Object.keys(row))),
        ).slice(0, 12);
        return (
          <article key={panel.key} style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <strong>{panel.displayName}</strong>
              <span style={muted}>{panel.status} · {panel.source}</span>
            </div>
            <p style={{ ...muted, margin: "4px 0 8px" }}>
              {panel.sourceTimestamp
                ? `Source ${new Date(panel.sourceTimestamp).toLocaleString()}`
                : "No source timestamp"}
              {panel.lagSeconds != null ? ` · lag ${panel.lagSeconds}s` : ""}
              {panel.reason ? ` · ${panel.reason}` : ""}
            </p>
            {panel.rows.length ? (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.78em" }}>
                  <thead>
                    <tr>
                      {columns.map((column) => (
                        <th key={column} style={{ textAlign: "left", padding: "4px 6px" }}>
                          {column}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {panel.rows.slice(0, 100).map((row, index) => (
                      <tr key={`${panel.key}-${index}`} style={{ borderTop: "1px solid var(--border)" }}>
                        {columns.map((column) => (
                          <td key={column} style={{ padding: "4px 6px" }}>
                            {formatOperationalValue(panel.key, row, column)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div style={muted}>No matching events; this is not a zero value.</div>
            )}
          </article>
        );
      })}
      {report.diagnosticsUrl ? (
        <a href={report.diagnosticsUrl} target="_blank" rel="noopener noreferrer">
          Open Azure diagnostics (new tab)
        </a>
      ) : null}
    </div>
  );
}

function formatOperationalValue(
  panelKey: string,
  row: Record<string, unknown>,
  column: string,
): string {
  if (panelKey !== "usage") {
    return row[column] == null ? "—" : String(row[column]);
  }
  const requests = Number(row.requests ?? 0);
  if (column === "tokens") {
    const tokens = Number(row.tokens ?? 0);
    const unknown = Number(row.unknownUsage ?? 0);
    if (requests > 0 && unknown >= requests && tokens === 0) return "Unknown";
    if (unknown > 0) {
      return `Known subtotal ${formatTokens(tokens)} (${Math.max(0, requests - unknown)}/${requests} requests reported)`;
    }
    return formatTokens(tokens);
  }
  if (column === "knownCostUsd") {
    const cost = Number(row.knownCostUsd ?? 0);
    const unknown = Number(row.unknownCost ?? 0);
    if (requests > 0 && unknown >= requests && cost === 0) return "Unknown";
    const formatted = `$${cost.toFixed(4)}`;
    return unknown > 0
      ? `Known subtotal ${formatted} (${Math.max(0, requests - unknown)}/${requests} requests reported)`
      : formatted;
  }
  return row[column] == null ? "—" : String(row[column]);
}

function ModelBars({ items }: { items: ModelUsageBucket[] }) {
  if (!items.length) return <div style={muted}>No usage in this window.</div>;
  const max = Math.max(...items.map((m) => m.totalTokens), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {items.slice(0, 8).map((m) => (
        <div key={m.model} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div
            style={{ width: 140, fontSize: "0.8em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            title={m.model}
          >
            {m.model}
          </div>
          <div style={{ flex: 1, background: "var(--bg)", borderRadius: 4, height: 18 }}>
            <div
              style={{
                width: `${barScale(m.totalTokens, max, 100)}%`,
                background: "var(--accent)",
                height: "100%",
                borderRadius: 4,
                minWidth: m.totalTokens > 0 ? 2 : 0,
              }}
            />
          </div>
          <div style={{ width: 96, textAlign: "right", fontSize: "0.8em" }}>
            {formatTokens(m.totalTokens)} · {formatUsd(m.costMicroUsd)}
          </div>
        </div>
      ))}
    </div>
  );
}

function DayTrend({ items }: { items: DayUsageBucket[] }) {
  if (!items.length) return <div style={muted}>No usage in this window.</div>;
  const W = 520;
  const H = 120;
  const values = items.map((d) => d.totalTokens);
  const points = linePoints(values, W, H);
  const peak = Math.max(...values, 0);
  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        height={H}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Tokens per day from ${items[0]?.day} to ${items[items.length - 1]?.day}; peak ${formatTokens(peak)} tokens per day`}
      >
        <polyline points={points} fill="none" stroke="var(--accent)" strokeWidth={2} />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", ...muted }}>
        <span>{items[0]?.day}</span>
        <span>peak {formatTokens(peak)} tok/day</span>
        <span>{items[items.length - 1]?.day}</span>
      </div>
    </div>
  );
}

function UserCell({
  displayName,
  email,
  identified,
  userId,
}: {
  displayName?: string | null;
  email?: string | null;
  identified: boolean;
  userId: string;
}) {
  const visibleName = identified ? displayName?.trim() : "";
  const visibleEmail = identified ? email : null;
  // The table always shows a shortened id (and, in identified mode, a name);
  // the untruncated id is otherwise unreachable without page source. A
  // hover-only title (even paired with tabIndex) never surfaces on keyboard
  // focus in any browser, so disclose it via the same focus/click/hover
  // affordance used elsewhere instead.
  const idHint = shortUserId(userId);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <span style={{ fontFamily: visibleName ? "inherit" : "monospace" }}>
          {userLabel(visibleName, userId)}
        </span>
        <HelpTooltip label={`full id for ${idHint}`} size="sm">
          Full id: {userId}
          {visibleEmail ? `, email: ${visibleEmail}` : null}
        </HelpTooltip>
      </span>
      {visibleName ? (
        <span style={{ fontFamily: "monospace", fontSize: "0.82em", color: "var(--fg-muted)" }}>
          {shortUserId(userId)}
        </span>
      ) : null}
      {visibleEmail ? (
        <span style={{ fontSize: "0.82em", color: "var(--fg-muted)" }}>{visibleEmail}</span>
      ) : null}
    </div>
  );
}

function TopUsers({ rows, identified }: { rows: AdminUserRow[]; identified: boolean }) {
  if (!rows.length) return <div style={muted}>No usage in this window.</div>;
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85em" }}>
      <thead>
        <tr style={{ textAlign: "left", color: "var(--fg-muted)" }}>
          <th style={{ padding: "4px 8px" }}>User</th>
          <th style={{ padding: "4px 8px", textAlign: "right" }}>Tokens</th>
          <th style={{ padding: "4px 8px", textAlign: "right" }}>Cost</th>
          <th style={{ padding: "4px 8px", textAlign: "right" }}>Reqs</th>
          <th style={{ padding: "4px 8px" }}>Entitlement</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((u) => (
          <tr key={u.userId} style={{ borderTop: "1px solid var(--border)" }}>
            <td style={{ padding: "4px 8px" }}>
              <UserCell displayName={u.displayName} email={u.email} identified={identified} userId={u.userId} />
            </td>
            <td style={{ padding: "4px 8px", textAlign: "right" }}>{formatTokens(u.totalTokens)}</td>
            <td style={{ padding: "4px 8px", textAlign: "right" }}>
              {u.costKnown ? formatUsd(u.costMicroUsd) : "—"}
            </td>
            <td style={{ padding: "4px 8px", textAlign: "right" }}>{u.requests}</td>
            <td style={{ padding: "4px 8px" }}>
              <span
                title={
                  u.entitlement
                    ? "Managed via PUT/DELETE /api/admin/entitlements/{userId}"
                    : "No override — shipped unlimited default"
                }
              >
                {entitlementLabel(u.entitlement)}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Agents({ items }: { items: AgentUsageBucket[] }) {
  if (!items.length) return <div style={muted}>No agent activity in this window.</div>;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {items.map((a) => {
        const errors = errorLabel(a.erroredRequests);
        return (
          <div key={a.agent} style={{ ...card, padding: "8px 12px" }}>
            <div style={{ fontWeight: 600 }}>{a.agent}</div>
            <div style={muted}>
              {formatTokens(a.totalTokens)} tok · {a.requests} reqs · {a.users} users
              {errors ? (
                <span style={{ color: "var(--danger)" }}> · {errors}</span>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function UserAgents({ rows, identified }: { rows: UserAgentBucket[]; identified: boolean }) {
  const groups = groupUserAgents(rows);
  if (!groups.length) return <div style={muted}>No agent activity in this window.</div>;
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85em" }}>
      <thead>
        <tr style={{ textAlign: "left", color: "var(--fg-muted)" }}>
          <th style={{ padding: "4px 8px" }}>User</th>
          <th style={{ padding: "4px 8px" }}>Agent</th>
          <th style={{ padding: "4px 8px", textAlign: "right" }}>Tokens</th>
          <th style={{ padding: "4px 8px", textAlign: "right" }}>Reqs</th>
          <th style={{ padding: "4px 8px", textAlign: "right" }}>Errors</th>
        </tr>
      </thead>
      <tbody>
        {groups.map((g) =>
          g.rows.map((r, i) => {
            const errors = errorLabel(r.erroredRequests);
            return (
              <tr key={`${g.userId}:${r.agent}`} style={{ borderTop: "1px solid var(--border)" }}>
                {i === 0 ? (
                  <td rowSpan={g.rows.length} style={{ padding: "4px 8px", verticalAlign: "top" }}>
                    <UserCell displayName={g.displayName} email={g.email} identified={identified} userId={g.userId} />
                  </td>
                ) : null}
                <td style={{ padding: "4px 8px" }}>{r.agent}</td>
                <td style={{ padding: "4px 8px", textAlign: "right" }}>{formatTokens(r.totalTokens)}</td>
                <td style={{ padding: "4px 8px", textAlign: "right" }}>{r.requests}</td>
                <td
                  style={{
                    padding: "4px 8px",
                    textAlign: "right",
                    color: errors ? "var(--danger)" : "var(--fg-muted)",
                  }}
                >
                  {r.erroredRequests || "—"}
                </td>
              </tr>
            );
          }),
        )}
      </tbody>
    </table>
  );
}

function DimBars({ items, emptyLabel, labelOf }: {
  items: DimensionBucket[];
  emptyLabel: string;
  labelOf?: (key: string) => string;
}) {
  if (!items.length) return <div style={muted}>{emptyLabel}</div>;
  const total = sumRequests(items);
  const max = Math.max(...items.map((d) => d.requests), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {items.slice(0, 8).map((d) => {
        const label = labelOf ? labelOf(d.key) : d.key;
        const errors = errorLabel(d.erroredRequests);
        return (
          <div key={d.key} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div
              style={{ width: 140, fontSize: "0.8em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              title={label}
            >
              {label}
            </div>
            <div style={{ flex: 1, background: "var(--bg)", borderRadius: 4, height: 18 }}>
              <div
                style={{
                  width: `${barScale(d.requests, max, 100)}%`,
                  background: "var(--accent)",
                  height: "100%",
                  borderRadius: 4,
                  minWidth: d.requests > 0 ? 2 : 0,
                }}
              />
            </div>
            <div style={{ width: 110, textAlign: "right", fontSize: "0.8em" }}>
              {d.requests} · {formatPercent(dimensionShare(d.requests, total))}
              {errors ? <span style={{ color: "var(--danger)" }}> · {errors}</span> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ResourcePanels({ panels }: { panels: ResourcePanel[] }) {
  if (!panels.length) return <div style={muted}>No resource metrics configured.</div>;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
      {panels.map((p) => (
        <div key={p.key} style={card}>
          <div style={{ fontWeight: 600 }}>{p.displayName}</div>
          {p.status === "ok" ? (
            <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0" }}>
              {p.metrics.map((m) => (
                <li key={m.name} style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85em", padding: "2px 0" }}>
                  <span style={muted}>{m.label}</span>
                  <span>
                    {m.value == null ? "—" : formatCompact(m.value)}
                    {m.unit ? ` ${m.unit}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div style={{ ...muted, marginTop: 8 }}>Unavailable — {p.detail}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function formatWhen(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

function authModeLabel(mode: string): string {
  if (mode === "api_key") return "API key";
  if (mode === "managed_identity") return "Managed identity";
  if (mode === "unconfigured") return "Unconfigured";
  return mode;
}

// Diagnostics for the fail-soft web-search path. The capability turns a
// categorized upstream failure into a clean {"error": ...} and continues, so a
// misconfiguration is otherwise invisible; this panel surfaces the categorized
// failures + the config posture (enabled / authMode) that explains them.
function WebSearchHealthPanel({ report }: { report: WebSearchHealthReport | null }) {
  if (!report) return <div style={muted}>Web search health is unavailable.</div>;
  const hint = webSearchHint(report);
  return (
    <div>
      <div
        style={{
          margin: "0 0 12px",
          fontSize: "0.85em",
          fontWeight: hint.tone === "warn" ? 600 : 400,
          color: hint.tone === "warn" ? "var(--danger)" : "var(--fg)",
        }}
      >
        {hint.text}
      </div>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 16,
          fontSize: "0.85em",
          marginBottom: 12,
        }}
      >
        <span>
          <strong>Feature:</strong> {report.enabled ? "Enabled" : "Disabled"}
        </span>
        <span>
          <strong>Auth mode:</strong> {authModeLabel(report.authMode)}
        </span>
        <span>
          <strong>Calls:</strong> {formatCompact(report.totalCalls)}
        </span>
        <span>
          <strong>Successes:</strong> {formatCompact(report.successes)}
        </span>
        <span>
          <strong>Failures:</strong> {formatCompact(report.failures)}
        </span>
        <span style={muted}>Last failure: {formatWhen(report.lastFailureAt)}</span>
      </div>
      {report.byCategory.length > 0 && (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "0 0 12px",
            display: "flex",
            flexWrap: "wrap",
            gap: 8,
          }}
        >
          {report.byCategory.map((c) => (
            <li
              key={c.category}
              style={{
                border: "1px solid var(--border)",
                borderRadius: "var(--radius)",
                padding: "2px 8px",
                fontSize: "0.8em",
              }}
            >
              {webSearchCategoryLabel(c.category)}: <strong>{c.count}</strong>
            </li>
          ))}
        </ul>
      )}
      {report.recent.length > 0 ? (
        <div>
          <div style={{ ...muted, marginBottom: 4 }}>
            Recent failures (this replica, newest first)
          </div>
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {report.recent.map((f, i) => (
              <li
                key={`${f.at}-${i}`}
                style={{
                  fontSize: "0.8em",
                  padding: "3px 0",
                  borderTop: i ? "1px solid var(--border)" : undefined,
                  display: "flex",
                  gap: 8,
                }}
              >
                <span style={{ color: "var(--danger)", flexShrink: 0, minWidth: 96 }}>
                  {webSearchCategoryLabel(f.category)}
                </span>
                <span style={{ flex: 1, wordBreak: "break-word" }}>{f.detail ?? "—"}</span>
                <span style={{ ...muted, flexShrink: 0 }}>{formatWhen(f.at)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div style={muted}>No recorded failures on this replica.</div>
      )}
    </div>
  );
}

export function AdminDashboard() {
  const [phase, setPhase] = useState<"checking" | "forbidden" | "ready">("checking");
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<DashboardData>(EMPTY);
  const loadGenerationRef = useRef(0);
  const loadAbortRef = useRef<AbortController | null>(null);
  const [identifyUsers, setIdentifyUsers] = useState(() => {
    try {
      return window.localStorage.getItem(IDENTITY_STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    let cancelled = false;
    fetchWhoAmI()
      .then((who) => {
        if (cancelled) return;
        setPhase(canShowAdmin(who) ? "ready" : "forbidden");
      })
      .catch(() => {
        if (!cancelled) setPhase("forbidden");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(IDENTITY_STORAGE_KEY, identifyUsers ? "true" : "false");
    } catch {
      /* localStorage can be unavailable in private browsing or tests */
    }
  }, [identifyUsers]);

  const load = useCallback(async (window: number, identify: boolean) => {
    const generation = ++loadGenerationRef.current;
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    setLoading(true);
    setData(EMPTY);
    setError(null);
    const [
      summary,
      byModel,
      byDay,
      byUser,
      agents,
      userAgents,
      distributions,
      resources,
      webSearch,
      operations,
      security,
    ] =
      await Promise.allSettled([
        fetchSummary(window, controller.signal),
        fetchByModel(window, controller.signal),
        fetchByDay(window, controller.signal),
        fetchByUser(window, 20, 0, identify, controller.signal),
        fetchAgents(window, controller.signal),
        fetchUserAgents(window, identify, controller.signal),
        fetchDistributions(window, controller.signal),
        fetchResources(controller.signal),
        fetchWebSearchHealth(controller.signal),
        fetchOperations(60, controller.signal),
        fetchSecurityMetrics(60, controller.signal),
      ]);
    if (controller.signal.aborted || generation !== loadGenerationRef.current) return;
    const next: DashboardData = { ...EMPTY };
    if (summary.status === "fulfilled") next.summary = summary.value;
    if (byModel.status === "fulfilled") next.byModel = byModel.value.byModel;
    if (byDay.status === "fulfilled") next.byDay = byDay.value.byDay;
    if (byUser.status === "fulfilled") {
      next.byUser = byUser.value.byUser;
      next.truncated = next.truncated || byUser.value.truncated;
    }
    if (agents.status === "fulfilled") next.agents = agents.value.agents;
    if (userAgents.status === "fulfilled") next.userAgents = userAgents.value.userAgents;
    if (distributions.status === "fulfilled") {
      next.byRegion = distributions.value.byRegion;
      next.byDataZone = distributions.value.byDataZone;
      next.byDeployment = distributions.value.byDeployment;
      next.byStatus = distributions.value.byStatus;
      next.truncated = next.truncated || distributions.value.truncated;
    }
    if (resources.status === "fulfilled") next.resources = resources.value.panels;
    if (webSearch.status === "fulfilled") next.webSearch = webSearch.value;
    if (operations.status === "fulfilled") next.operations = operations.value;
    if (security.status === "fulfilled") next.security = security.value;
    if (summary.status === "fulfilled") next.truncated = next.truncated || summary.value.truncated;
    const namedResults = [
      ["usage summary", summary],
      ["model usage", byModel],
      ["daily usage", byDay],
      ["users", byUser],
      ["agents", agents],
      ["user agents", userAgents],
      ["distributions", distributions],
      ["resources", resources],
      ["web search", webSearch],
      ["operations", operations],
      ["security", security],
    ] as const;
    next.loadErrors = namedResults
      .filter(([, result]) => result.status === "rejected")
      .map(([name, result]) =>
        `${name}: ${
          result.status === "rejected" && result.reason instanceof Error
            ? result.reason.message
            : "unavailable"
        }`,
      );
    if (next.loadErrors.length) setError("Some admin data sources failed to load.");
    setData(next);
    setLoading(false);
  }, []);

  useEffect(() => {
    if (phase !== "ready") return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-filter-change; setState only runs after `load`'s awaited requests settle, not synchronously
    void load(days, identifyUsers);
    return () => loadAbortRef.current?.abort();
  }, [phase, days, identifyUsers, load]);

  if (phase === "checking") {
    return <Shell>Checking access…</Shell>;
  }
  if (phase === "forbidden") {
    return (
      <Shell>
        <div style={card}>
          <h2 style={{ marginTop: 0 }}>Admins only</h2>
          <p style={muted}>
            This dashboard is restricted to application administrators. If you believe you should have
            access, contact the app owner.
          </p>
          <Link href="/" style={{ color: "var(--accent)" }}>
            ← Back to chat
          </Link>
        </div>
      </Shell>
    );
  }

  const s = data.summary;
  return (
    <Shell>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <h1 style={{ fontSize: "1.3em", margin: 0, flex: 1 }}>Usage dashboard</h1>
        <span style={{ ...muted, display: "flex", alignItems: "center", gap: 4 }}>
          <input
            type="checkbox"
            id="admin-identify-users"
            checked={identifyUsers}
            onChange={(e) => setIdentifyUsers(e.target.checked)}
          />
          <label htmlFor="admin-identify-users">Show real identities</label>
          <HelpTooltip label="Show real identities" size="sm">
            On resolves each user&apos;s hashed id to their real display name and
            email (an extra directory lookup per row) and sends that to your
            browser. Off never fetches or sends that PII — rows stay hash-only,
            which is safer for demos, screen-shares, or recordings. This
            preference is remembered on this device only.
          </HelpTooltip>
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <label style={muted} htmlFor="admin-window">
            Window
          </label>
          <HelpTooltip label="Window" size="sm">
            How far back to aggregate usage. Wider windows take longer to load
            and are more likely to hit the per-window record cap, which
            silently turns totals into a lower bound (watch for the ⚠ banner
            below).
          </HelpTooltip>
        </span>
        <select
          id="admin-window"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          style={{
            background: "var(--bg-elevated)",
            color: "var(--fg)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "4px 8px",
          }}
        >
          {WINDOWS.map((w) => (
            <option key={w} value={w}>
              Last {w} days
            </option>
          ))}
        </select>
        <Link href="/" style={{ ...muted, color: "var(--accent)", textDecoration: "none" }}>
          ← Chat
        </Link>
      </div>

      {error ? (
        <div role="alert" style={{ ...card, borderColor: "var(--danger)", marginBottom: 16, color: "var(--danger)" }}>
          {error}
          {data.loadErrors.length ? (
            <ul>{data.loadErrors.map((message) => <li key={message}>{message}</li>)}</ul>
          ) : null}
        </div>
      ) : null}
      {data.truncated ? (
        <div style={{ ...card, marginBottom: 16 }}>
          <span style={muted}>
            ⚠ This window has more usage records than the dashboard aggregates
            at once, so results were capped — totals below are a lower bound,
            not the true total. Pick a shorter window for exact numbers.
          </span>
        </div>
      ) : null}

      {loading ? (
        <div
          role="status"
          aria-live="polite"
          aria-label={`Loading dashboard data for the last ${days} days`}
          style={{ ...card, ...muted }}
        >
          Loading dashboard data for the last {days} days…
        </div>
      ) : (
      <>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
          gap: 12,
          marginBottom: 20,
        }}
      >
        <StatCard label="Active users" value={s ? formatCompact(s.activeUsers) : "—"} />
        <StatCard
          label="Tokens"
          value={
            s
              ? s.totalRequests > 0 &&
                s.unknownUsageRequests >= s.totalRequests &&
                s.totalTokens === 0
                ? "Unknown"
                : s.unknownUsageRequests > 0
                  ? `Known subtotal ${formatTokens(s.totalTokens)}`
                  : formatTokens(s.totalTokens)
              : "—"
          }
          sub={
            s
              ? s.unknownUsageRequests > 0
                ? `${Math.max(0, s.totalRequests - s.unknownUsageRequests)}/${s.totalRequests} requests reported`
                : `${formatTokens(s.totalPromptTokens)} in · ${formatTokens(s.totalCompletionTokens)} out`
              : "Usage unavailable"
          }
        />
        <StatCard
          label="Cost"
          value={
            s
              ? s.totalRequests > 0 &&
                s.costUnknownRequests >= s.totalRequests &&
                s.totalCostMicroUsd === 0
                ? "Unknown"
                : s.costUnknownRequests > 0
                  ? `Known subtotal ${formatUsd(s.totalCostMicroUsd)}`
                  : formatUsd(s.totalCostMicroUsd)
              : "—"
          }
          sub={
            s?.costUnknownRequests
              ? `${s.costUnknownRequests} request${s.costUnknownRequests === 1 ? "" : "s"} unknown`
              : s?.currency ?? "USD"
          }
        />
        <StatCard
          label="Requests"
          value={s ? formatCompact(s.totalRequests) : "—"}
          sub={s ? `${formatPercent(s.errorRate)} errors` : "Status unavailable"}
        />
        <StatCard
          label="Models / agents"
          value={s ? `${s.distinctModels} / ${s.distinctAgents}` : "—"}
        />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 20 }}>
        <section style={card}>
          <h2 style={sectionTitle}>Tokens by model</h2>
          <ModelBars items={data.byModel} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Tokens by day</h2>
          <DayTrend items={data.byDay} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Top users</h2>
          <TopUsers rows={data.byUser} identified={identifyUsers} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Agents in use</h2>
          <Agents items={data.agents} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Who uses which agents</h2>
          <UserAgents rows={data.userAgents} identified={identifyUsers} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Requests by region</h2>
          <DimBars items={data.byRegion} emptyLabel="No region data in this window." />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Requests by data zone</h2>
          <DimBars items={data.byDataZone} emptyLabel="No data-zone data in this window." />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Requests by deployment</h2>
          <DimBars items={data.byDeployment} emptyLabel="No deployment data in this window." />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Request status mix</h2>
          <DimBars items={data.byStatus} emptyLabel="No requests in this window." labelOf={statusLabel} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Platform resources</h2>
          <p style={{ ...muted, margin: "-4px 0 12px" }}>
            Live Azure Monitor values for the last hour. Unavailable or — means the
            source is not configured, fresh, or reporting; it never means zero.
          </p>
          <ResourcePanels panels={data.resources} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Operations and latency</h2>
          <OperationalPanels
            report={data.operations}
            emptyLabel="Operations telemetry is unavailable."
          />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Security and governance blocks</h2>
          <OperationalPanels
            report={data.security}
            emptyLabel="Security telemetry is unavailable."
          />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Web search health</h2>
          <p style={{ ...muted, margin: "-4px 0 12px" }}>
            Diagnoses the fail-soft web-search path. Counters are per-replica and in-memory
            (reset on restart); the durable, cross-replica view is App Insights.
          </p>
          <WebSearchHealthPanel report={data.webSearch} />
        </section>
      </div>
      </>
      )}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main
      id="main"
      style={{
        maxWidth: 920,
        margin: "0 auto",
        padding: "24px max(16px, 4%)",
        color: "var(--fg)",
        minHeight: "100vh",
      }}
    >
      {children}
    </main>
  );
}
