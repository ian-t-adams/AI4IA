"use client";

// Admin usage dashboard. Client component that:
//  1. Reads exact server-owned admin operations from /api/admin/whoami. The API
//     still re-authorizes every request; an admin badge never grants all panels.
//  2. Uses ONE usage scan: the consolidated overview when entitlement reads are
//     allowed, otherwise an unenriched summary. Separately authorized resource,
//     operations, security and web-search panels load alongside it
//     (Promise.allSettled) so one failing source never blanks the page.
//
// The single usage request is deliberate. This used to fan out to seven admin
// usage endpoints at once, and each one independently scanned up to 50,000 full
// ledger rows for the same window — roughly seven copies resident at once in an
// API replica capped at 1 GiB that is also serving chat (audit P1-15). The
// consolidated endpoint scans once and reports `partialSections` for any rollup
// that failed, so panels still degrade independently rather than all-or-nothing.
// All display logic lives in pure helpers in lib/admin.ts (unit-tested); this file
// is presentation only. Charts are inline SVG (no charting dependency).
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import Link from "next/link";

import { HelpTooltip } from "./HelpTooltip";
import { secondaryBtn } from "./builderStyles";
import {
  type AdminDashboardAccess,
  type AdminUsageSummary,
  type AdminUserRow,
  type AgentUsageBucket,
  type DayUsageBucket,
  type DimensionBucket,
  type ModelUsageBucket,
  type OfficialMcpHealthReport,
  type OperationalMetricsReport,
  type OperationalPanel,
  type ResourcePanel,
  type UserAgentBucket,
  type WebSearchHealthReport,
  OVERVIEW_SECTION_LABELS,
  barScale,
  rankModelBuckets,
  canShowAdmin,
  adminDashboardAccess,
  dimensionShare,
  entitlementLabel,
  errorLabel,
  fetchOverview,
  fetchResources,
  fetchUsageSummary,
  fetchOfficialMcpHealth,
  fetchOperations,
  fetchSecurityMetrics,
  fetchWebSearchHealth,
  fetchWhoAmI,
  formatCompact,
  formatPercent,
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
const IDENTITY_CHANGE_EVENT = "ai4ia-admin-identity-preference";
const USER_PAGE_SIZE = 20;
// Panels served by the one usage request. Named individually so a failure of that
// request still reports which panels are affected, exactly as the seven separate
// requests did.
const USAGE_PANELS = [
  "usage summary",
  "model usage",
  "daily usage",
  "users",
  "agents",
  "user agents",
  "distributions",
] as const;

function readIdentityPreference(): boolean {
  try {
    return window.localStorage.getItem(IDENTITY_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function subscribeIdentityPreference(onChange: () => void): () => void {
  const handleStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === IDENTITY_STORAGE_KEY) onChange();
  };
  const handleLocalChange = () => onChange();
  window.addEventListener("storage", handleStorage);
  window.addEventListener(IDENTITY_CHANGE_EVENT, handleLocalChange);
  return () => {
    window.removeEventListener("storage", handleStorage);
    window.removeEventListener(IDENTITY_CHANGE_EVENT, handleLocalChange);
  };
}

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
      return `Known subtotal ${formatCompact(tokens)} (${Math.max(0, requests - unknown)}/${requests} requests reported)`;
    }
    return formatCompact(tokens);
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
  const shown = rankModelBuckets(items, 8);
  const max = Math.max(...shown.map((m) => m.totalTokens), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {shown.map((m) => (
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
            {formatCompact(m.totalTokens)} · {formatUsd(m.costMicroUsd)}
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
        aria-label={`Tokens per day from ${items[0]?.day} to ${items[items.length - 1]?.day}; peak ${formatCompact(peak)} tokens per day`}
      >
        <polyline points={points} fill="none" stroke="var(--accent)" strokeWidth={2} />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", ...muted }}>
        <span>{items[0]?.day}</span>
        <span>peak {formatCompact(peak)} tok/day</span>
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

function TopUsers({ rows, identified, canManage }: {
  rows: AdminUserRow[]; identified: boolean; canManage: boolean;
}) {
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
            <td style={{ padding: "4px 8px", textAlign: "right" }}>{formatCompact(u.totalTokens)}</td>
            <td style={{ padding: "4px 8px", textAlign: "right" }}>
              {u.costKnown ? formatUsd(u.costMicroUsd) : "—"}
            </td>
            <td style={{ padding: "4px 8px", textAlign: "right" }}>{u.requests}</td>
            <td style={{ padding: "4px 8px" }}>
              <span
                title={
                  u.entitlement
                    ? canManage
                      ? "Managed via PUT/DELETE /api/admin/entitlements/{userId}"
                      : "Read-only entitlement. Changes require entitlement-write access."
                    : u.entitlementKnown === false
                      ? "Entitlement unavailable"
                      : "No override — shipped unlimited default"
                }
              >
                {entitlementLabel(u.entitlement, u.entitlementKnown)}
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
              {formatCompact(a.totalTokens)} tok · {a.requests} reqs · {a.users} users
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
                <td style={{ padding: "4px 8px", textAlign: "right" }}>{formatCompact(r.totalTokens)}</td>
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
          {p.status === "ok" || p.status === "partial" ? (
            <>
              {p.status === "partial" ? (
                <div style={{ color: "var(--danger)", fontSize: "0.8em", marginTop: 4 }}>
                  Partial{p.detail ? ` — ${p.detail}` : ""}
                </div>
              ) : null}
              <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0" }}>
                {p.metrics.map((m) => (
                  <li key={m.name} style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85em", padding: "2px 0" }}>
                    <span style={muted}>{m.label}</span>
                    {m.error ? (
                      <span
                        style={{ color: "var(--danger)" }}
                        title={
                          m.errorCode
                            ? `${m.errorCode}${m.errorMessage ? `: ${m.errorMessage}` : ""}`
                            : m.error
                        }
                      >
                        Unavailable{m.errorCode ? ` (${m.errorCode})` : ""}
                      </span>
                    ) : (
                      <span>
                        {m.value == null ? "—" : formatCompact(m.value)}
                        {m.unit ? ` ${m.unit}` : ""}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </>
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

type McpHealthState = { phase: "loading"; refreshing: boolean } | { phase: "ready"; report: OfficialMcpHealthReport } |
  { phase: "error"; message: string };

function OfficialMcpHealthPanel({ canRefresh }: { canRefresh: boolean }) {
  const [state, setState] = useState<McpHealthState>({ phase: "loading", refreshing: false });
  const active = useRef<AbortController | null>(null);
  const load = useCallback((refresh: boolean, controller: AbortController) => {
    active.current = controller;
    return fetchOfficialMcpHealth(refresh, controller.signal).then(
      (report) => { if (!controller.signal.aborted) setState({ phase: "ready", report }); },
      (error: unknown) => {
        if (!controller.signal.aborted) setState({ phase: "error", message: error instanceof Error ? error.message : "Inspection unavailable." });
      },
    ).finally(() => { if (active.current === controller) active.current = null; });
  }, []);
  useEffect(() => {
    void load(false, new AbortController());
    return () => { active.current?.abort(); };
  }, [load]);
  const inspect = (refresh: boolean) => {
    if (active.current || (refresh && !canRefresh)) return;
    setState({ phase: "loading", refreshing: refresh });
    void load(refresh, new AbortController());
  };
  return <div>
    <p style={muted}>Official catalog discovery for this replica. Inspection does not clear the discovery cache.</p>
    {state.phase === "loading" && <p role="status" style={muted}>
      {state.refreshing ? "Refreshing MCP discovery..." : "Inspecting official MCP..."}
    </p>}
    {state.phase === "error" && <p role="alert" style={{ ...muted, color: "var(--danger)" }}>{state.message}</p>}
    {state.phase === "ready" && <>
      <p style={muted}>Observed {formatWhen(state.report.generatedAt)}.</p>
      {state.report.enabled === false ? <p style={muted}>Official MCP is disabled.</p> :
        state.report.gatewayConfigured === false ? <p style={muted}>The official MCP gateway is not configured.</p> :
          state.report.servers.length === 0 ? <p style={muted}>No official MCP servers in the catalog.</p> :
            <ul style={{ paddingLeft: 20 }}>
              {state.report.servers.map((server) => <li key={server.name}>
                <strong>{server.displayName || server.name}</strong>: {server.toolCount} tools
                <p style={muted}>Last connected: {formatWhen(server.lastConnectedAt)}</p>
                {server.lastError && <p role="alert" style={{ ...muted, color: "var(--danger)" }}>{server.lastError}</p>}
              </li>)}
            </ul>}
    </>}
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      <button type="button" style={secondaryBtn} disabled={state.phase === "loading"} onClick={() => inspect(false)}>
        {state.phase === "error" ? "Retry MCP inspection" : "Inspect official MCP"}
      </button>
      {canRefresh && <button type="button" style={secondaryBtn} disabled={state.phase === "loading"} onClick={() => inspect(true)}>
        Refresh MCP discovery cache
      </button>}
    </div>
    {canRefresh && <p style={muted}>Cache refresh is a separate explicit action, not an automatic retry after inspection fails.</p>}
  </div>;
}

export function AdminDashboard() {
  const [phase, setPhase] = useState<"checking" | "forbidden" | "error" | "ready">("checking");
  const [access, setAccess] = useState<AdminDashboardAccess>(() => adminDashboardAccess(null));
  const [accessAttempt, setAccessAttempt] = useState(0);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<DashboardData>(EMPTY);
  const loadGenerationRef = useRef(0);
  const loadAbortRef = useRef<AbortController | null>(null);
  const identifyPreference = useSyncExternalStore(
    subscribeIdentityPreference,
    readIdentityPreference,
    () => false,
  );
  const identifyUsers = access.identify && identifyPreference;

  useEffect(() => {
    let cancelled = false;
    fetchWhoAmI()
      .then((who) => {
        if (cancelled) return;
        setAccess(adminDashboardAccess(who));
        setPhase(canShowAdmin(who) ? "ready" : "forbidden");
      })
      .catch(() => {
        if (!cancelled) setPhase("error");
      });
    return () => {
      cancelled = true;
    };
  }, [accessAttempt]);

  const setIdentifyPreference = useCallback((identify: boolean) => {
    try {
      window.localStorage.setItem(
        IDENTITY_STORAGE_KEY,
        identify ? "true" : "false",
      );
      window.dispatchEvent(new Event(IDENTITY_CHANGE_EVENT));
    } catch {
      /* localStorage can be unavailable in private browsing or tests */
    }
  }, []);

  const load = useCallback(async (windowDays: number, identify: boolean) => {
    const generation = ++loadGenerationRef.current;
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    setLoading(true);
    setData(EMPTY);
    setError(null);
    const reads: { panels: readonly string[]; promise: Promise<Partial<DashboardData>> }[] = [];
    if (access.overview) {
      reads.push({
        panels: USAGE_PANELS,
        promise: fetchOverview(windowDays, USER_PAGE_SIZE, 0, identify && access.identify, controller.signal).then((report) => ({
          summary: report.summary, byModel: report.byModel, byDay: report.byDay, byUser: report.byUser,
          agents: report.agents, userAgents: report.userAgents, byRegion: report.byRegion, byDataZone: report.byDataZone,
          byDeployment: report.byDeployment, byStatus: report.byStatus, truncated: report.truncated,
          loadErrors: (report.partialSections ?? []).map((section) => `${OVERVIEW_SECTION_LABELS[section] ?? section}: unavailable`),
        })),
      });
    } else if (access.usage) {
      reads.push({ panels: ["usage summary"], promise: fetchUsageSummary(windowDays, controller.signal)
        .then((summary) => ({ summary, truncated: summary.truncated })) });
    }
    if (access.resources) {
      reads.push({ panels: ["resources"], promise: fetchResources(controller.signal).then((report) => ({ resources: report.panels })) });
    }
    if (access.webSearch) {
      reads.push({ panels: ["web search"], promise: fetchWebSearchHealth(controller.signal).then((webSearch) => ({ webSearch })) });
    }
    if (access.operations) {
      reads.push({ panels: ["operations"], promise: fetchOperations(60, controller.signal).then((operations) => ({ operations })) });
    }
    if (access.security) {
      reads.push({ panels: ["security"], promise: fetchSecurityMetrics(60, controller.signal).then((security) => ({ security })) });
    }
    const results = await Promise.allSettled(reads.map((read) => read.promise));
    if (controller.signal.aborted || generation !== loadGenerationRef.current) return;
    const next: DashboardData = { ...EMPTY };
    const failures: string[] = [];
    for (const [index, result] of results.entries()) {
      if (result.status === "fulfilled") {
        Object.assign(next, result.value);
        failures.push(...(result.value.loadErrors ?? []));
      } else {
        const reason = result.reason instanceof Error ? result.reason.message : "unavailable";
        for (const panel of reads[index].panels) failures.push(`${panel}: ${reason}`);
      }
    }
    next.loadErrors = failures;
    if (next.loadErrors.length) setError("Some admin data sources failed to load.");
    setData(next);
    setLoading(false);
  }, [access]);

  useEffect(() => {
    if (phase !== "ready") return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-filter-change; setState only runs after `load`'s awaited requests settle, not synchronously
    void load(days, identifyUsers);
    return () => loadAbortRef.current?.abort();
  }, [phase, days, identifyUsers, load]);

  if (phase === "checking") {
    return <Shell>Checking access…</Shell>;
  }
  if (phase === "error") {
    return (
      <Shell>
        <div role="alert" style={card}>
          <h2 style={{ marginTop: 0 }}>Unable to verify admin access</h2>
          <p style={muted}>
            The access check could not be completed. Try again before assuming
            this account is not an administrator.
          </p>
          <button
            type="button"
            onClick={() => {
              setPhase("checking");
              setAccessAttempt((value) => value + 1);
            }}
            style={{
              minHeight: 44,
              padding: "8px 14px",
              border: "none",
              borderRadius: 8,
              background: "var(--accent)",
              color: "var(--accent-fg)",
              font: "inherit",
              fontWeight: 650,
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        </div>
      </Shell>
    );
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
        <h1 style={{ fontSize: "1.3em", margin: 0, flex: 1 }}>{access.usage ? "Usage dashboard" : "Admin dashboard"}</h1>
        {access.identify && <span style={{ ...muted, display: "flex", alignItems: "center", gap: 4 }}>
          <input
            type="checkbox"
            id="admin-identify-users"
            checked={identifyUsers}
            onChange={(e) => setIdentifyPreference(e.target.checked)}
          />
          <label htmlFor="admin-identify-users">Show real identities</label>
          <HelpTooltip label="Show real identities" size="sm">
            On resolves each user&apos;s hashed id to their real display name and
            email (an extra directory lookup per row) and sends that to your
            browser. Off never fetches or sends that PII — rows stay hash-only,
            which is safer for demos, screen-shares, or recordings. This
            preference is remembered on this device only.
          </HelpTooltip>
        </span>}
        {access.usage && <>
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
        </>}
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
          aria-label={access.usage ? `Loading dashboard data for the last ${days} days` : "Loading authorized dashboard panels"}
          style={{ ...card, ...muted }}
        >
          {access.usage ? `Loading dashboard data for the last ${days} days…` : "Loading authorized dashboard panels..."}
        </div>
      ) : (
      <>
      {access.usage && <div
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
                  ? `Known subtotal ${formatCompact(s.totalTokens)}`
                  : formatCompact(s.totalTokens)
              : "—"
          }
          sub={
            s
              ? s.unknownUsageRequests > 0
                ? `${Math.max(0, s.totalRequests - s.unknownUsageRequests)}/${s.totalRequests} requests reported`
                : `${formatCompact(s.totalPromptTokens)} in · ${formatCompact(s.totalCompletionTokens)} out`
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
      </div>}

      {access.usage && !access.overview && <p style={muted}>
        Showing unenriched usage totals. The consolidated breakdowns and entitlement-enriched user rows require
        {" "}<code>admin.entitlements.read</code> in addition to usage access.
      </p>}

      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 20 }}>
        {access.overview && <>
        <section style={card}>
          <h2 style={sectionTitle}>Top models by tokens and cost</h2>
          <ModelBars items={data.byModel} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Tokens by day</h2>
          <DayTrend items={data.byDay} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Top users</h2>
          <TopUsers rows={data.byUser} identified={identifyUsers} canManage={access.entitlementsWrite} />
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
        </>}

        {access.resources && <section style={card}>
          <h2 style={sectionTitle}>Platform resources</h2>
          <p style={{ ...muted, margin: "-4px 0 12px" }}>
            Live Azure Monitor values for the last hour. Unavailable or — means the
            source is not configured, fresh, or reporting; it never means zero.
          </p>
          <ResourcePanels panels={data.resources} />
        </section>}

        {access.operations && <section style={card}>
          <h2 style={sectionTitle}>Operations and latency</h2>
          <OperationalPanels
            report={data.operations}
            emptyLabel="Operations telemetry is unavailable."
          />
        </section>}

        {access.security && <section style={card}>
          <h2 style={sectionTitle}>Security and governance blocks</h2>
          <OperationalPanels
            report={data.security}
            emptyLabel="Security telemetry is unavailable."
          />
        </section>}

        {access.webSearch && <section style={card}>
          <h2 style={sectionTitle}>Web search health</h2>
          <p style={{ ...muted, margin: "-4px 0 12px" }}>
            Diagnoses the fail-soft web-search path. Counters are per-replica and in-memory
            (reset on restart); the durable, cross-replica view is App Insights.
          </p>
          <WebSearchHealthPanel report={data.webSearch} />
        </section>}
        {access.officialMcp && <section style={card}>
          <h2 style={sectionTitle}>Official MCP discovery</h2>
          <OfficialMcpHealthPanel canRefresh={access.refreshOfficialMcp} />
        </section>}
        {!access.usage && !access.resources && !access.operations && !access.security && !access.webSearch && !access.officialMcp &&
          <p style={muted}>No dashboard read panels are available for your admin operations.</p>}
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
