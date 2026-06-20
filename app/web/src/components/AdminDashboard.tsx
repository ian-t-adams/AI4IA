"use client";

// Admin usage dashboard (WS4, Part C). Client component that:
//  1. Confirms the viewer is an admin via /api/admin/whoami (cosmetic — the API
//     still enforces require_admin, so a non-admin only ever sees the forbidden
//     view and empty 403s).
//  2. Loads org-level rollups (summary / by-model / by-day / top-users / agents)
//     and best-effort resource panels for a selectable window, each independently
//     (Promise.allSettled) so one failing panel never blanks the page.
// All display logic lives in pure helpers in lib/admin.ts (unit-tested); this file
// is presentation only. Charts are inline SVG (no charting dependency).
import { useCallback, useEffect, useState } from "react";

import {
  type AdminUsageSummary,
  type AdminUserRow,
  type AgentUsageBucket,
  type DayUsageBucket,
  type ModelUsageBucket,
  type ResourcePanel,
  barScale,
  canShowAdmin,
  entitlementLabel,
  fetchAgents,
  fetchByDay,
  fetchByModel,
  fetchByUser,
  fetchResources,
  fetchSummary,
  fetchWhoAmI,
  formatCompact,
  formatPercent,
  formatTokens,
  formatUsd,
  linePoints,
  shortUserId,
} from "@/lib/admin";

const WINDOWS = [7, 30, 90];

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
  resources: ResourcePanel[];
  truncated: boolean;
}

const EMPTY: DashboardData = {
  summary: null,
  byModel: [],
  byDay: [],
  byUser: [],
  agents: [],
  resources: [],
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

function TopUsers({ rows }: { rows: AdminUserRow[] }) {
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
            <td style={{ padding: "4px 8px", fontFamily: "monospace" }} title={u.userId}>
              {shortUserId(u.userId)}
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
      {items.map((a) => (
        <div key={a.agent} style={{ ...card, padding: "8px 12px" }}>
          <div style={{ fontWeight: 600 }}>{a.agent}</div>
          <div style={muted}>
            {formatTokens(a.totalTokens)} tok · {a.requests} reqs · {a.users} users
          </div>
        </div>
      ))}
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

export function AdminDashboard() {
  const [phase, setPhase] = useState<"checking" | "forbidden" | "ready">("checking");
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<DashboardData>(EMPTY);

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

  const load = useCallback(async (window: number) => {
    setLoading(true);
    setError(null);
    const [summary, byModel, byDay, byUser, agents, resources] = await Promise.allSettled([
      fetchSummary(window),
      fetchByModel(window),
      fetchByDay(window),
      fetchByUser(window, 20, 0),
      fetchAgents(window),
      fetchResources(),
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
    if (resources.status === "fulfilled") next.resources = resources.value.panels;
    if (summary.status === "fulfilled") next.truncated = next.truncated || summary.value.truncated;
    if (summary.status === "rejected") {
      setError("Failed to load usage summary. Some panels may be empty.");
    }
    setData(next);
    setLoading(false);
  }, []);

  useEffect(() => {
    if (phase !== "ready") return;
    void load(days);
  }, [phase, days, load]);

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
          <a href="/" style={{ color: "var(--accent)" }}>
            ← Back to chat
          </a>
        </div>
      </Shell>
    );
  }

  const s = data.summary;
  return (
    <Shell>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <h1 style={{ fontSize: "1.3em", margin: 0, flex: 1 }}>Usage dashboard</h1>
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
        <a href="/" style={{ ...muted, color: "var(--accent)", textDecoration: "none" }}>
          ← Chat
        </a>
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
          <TopUsers rows={data.byUser} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Agents in use</h2>
          <Agents items={data.agents} />
        </section>

        <section style={card}>
          <h2 style={sectionTitle}>Platform resources</h2>
          <ResourcePanels panels={data.resources} />
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
