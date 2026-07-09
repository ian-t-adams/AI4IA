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
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import {
  type AdminUsageSummary,
  type AdminUserRow,
  type AgentUsageBucket,
  type DayUsageBucket,
  type DimensionBucket,
  type ModelUsageBucket,
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
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none" role="img" aria-label="Tokens per day">
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
  // Full hash (and email in identified mode) stay available on hover; the hash is the stable key.
  const tooltip = visibleEmail ? `${userId}\n${visibleEmail}` : userId;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }} title={tooltip}>
      <span style={{ fontFamily: visibleName ? "inherit" : "monospace" }}>
        {userLabel(visibleName, userId)}
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
                <td style={{ padding: "4px 8px" }}>
                  {i === 0 ? (
                    <UserCell displayName={g.displayName} email={g.email} identified={identified} userId={g.userId} />
                  ) : null}
                </td>
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
    setLoading(true);
    setError(null);
    const [summary, byModel, byDay, byUser, agents, userAgents, distributions, resources, webSearch] =
      await Promise.allSettled([
        fetchSummary(window),
        fetchByModel(window),
        fetchByDay(window),
        fetchByUser(window, 20, 0, identify),
        fetchAgents(window),
        fetchUserAgents(window, identify),
        fetchDistributions(window),
        fetchResources(),
        fetchWebSearchHealth(),
      ]);
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
    if (summary.status === "fulfilled") next.truncated = next.truncated || summary.value.truncated;
    if (summary.status === "rejected") {
      setError("Failed to load usage summary. Some panels may be empty.");
    }
    setData(next);
    setLoading(false);
  }, []);

  useEffect(() => {
    if (phase !== "ready") return;
    void load(days, identifyUsers);
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
        <label
          style={{ ...muted, display: "flex", alignItems: "center", gap: 6 }}
          title="Off keeps user rows hash-only for demos and screen-shares."
        >
          <input
            type="checkbox"
            checked={identifyUsers}
            onChange={(e) => setIdentifyUsers(e.target.checked)}
            aria-label="Show real identities"
          />
          Show real identities
        </label>
        <label style={muted} htmlFor="admin-window">
          Window
        </label>
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
        </div>
      ) : null}
      {data.truncated ? (
        <div style={{ ...card, marginBottom: 16 }}>
          <span style={muted}>
            ⚠ Results were capped for this window — totals are a lower bound.
          </span>
        </div>
      ) : null}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
          gap: 12,
          marginBottom: 20,
        }}
      >
        <StatCard label="Active users" value={formatCompact(s?.activeUsers ?? 0)} />
        <StatCard
          label="Tokens"
          value={formatTokens(s?.totalTokens ?? 0)}
          sub={`${formatTokens(s?.totalPromptTokens ?? 0)} in · ${formatTokens(s?.totalCompletionTokens ?? 0)} out`}
        />
        <StatCard label="Est. cost" value={formatUsd(s?.totalCostMicroUsd ?? 0)} sub={s?.currency ?? "USD"} />
        <StatCard
          label="Requests"
          value={formatCompact(s?.totalRequests ?? 0)}
          sub={`${formatPercent(s?.errorRate ?? 0)} errors`}
        />
        <StatCard
          label="Models / agents"
          value={`${s?.distinctModels ?? 0} / ${s?.distinctAgents ?? 0}`}
        />
      </div>

      {loading ? <div style={muted}>Loading…</div> : null}

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
          <ResourcePanels panels={data.resources} />
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
