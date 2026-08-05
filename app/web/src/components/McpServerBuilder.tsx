"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/api";
import {
  approvalPosture,
  healthBadge,
  MCP_AUTH_MODES,
  MCP_MAX_DESCRIPTION_LEN,
  MCP_MAX_DISPLAY_NAME_LEN,
  MCP_TOOL_APPROVALS,
  mcpEndpointError,
  mcpSecretError,
  mcpServerNameError,
  quarantineReason,
  toolApprovalPosture,
  type McpAuthMode,
  type McpToolApproval,
  type UserMcpServer,
} from "@/lib/customTools";
import { HelpTooltip } from "./HelpTooltip";
import { Pill, pillToneColor } from "./Pill";
import {
  checkRow,
  fieldset,
  iconBtn,
  inputStyle,
  labelStyle,
  primaryBtn,
  secondaryBtn,
} from "./builderStyles";

interface ServerForm {
  name: string;
  displayName: string;
  description: string;
  endpoint: string;
  authMode: McpAuthMode;
  secret: string;
  trusted: boolean;
  enabled: boolean;
}

function blankForm(): ServerForm {
  return {
    name: "",
    displayName: "",
    description: "",
    endpoint: "",
    authMode: "none",
    secret: "",
    trusted: false,
    enabled: true,
  };
}

// The server never returns the secret, so it always starts blank on edit. Leaving
// it blank for an authed server reuses the durably stored credential.
function formFrom(s: UserMcpServer): ServerForm {
  return {
    name: s.name,
    displayName: s.displayName,
    description: s.description,
    endpoint: s.endpoint,
    authMode: s.authMode,
    secret: "",
    trusted: s.trusted,
    enabled: s.enabled,
  };
}

export function McpServerBuilder({ onChanged }: { onChanged?: () => void }) {
  const [mine, setMine] = useState<UserMcpServer[]>([]);
  const [editing, setEditing] = useState<string | null>(null); // name, or null = new
  const [form, setForm] = useState<ServerForm>(blankForm);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const current = editing ? mine.find((s) => s.name === editing) ?? null : null;

  const refreshMine = useCallback(async () => {
    try {
      setMine(await api.listMcpServers());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refreshMine();
  }, [refreshMine]);

  const startNew = useCallback(() => {
    setEditing(null);
    setForm(blankForm());
    setError(null);
    setNotice(null);
  }, []);

  const startEdit = useCallback((s: UserMcpServer) => {
    setEditing(s.name);
    setForm(formFrom(s));
    setError(null);
    setNotice(null);
  }, []);

  const submit = useCallback(async () => {
    setError(null);
    setNotice(null);
    if (!editing) {
      const ne = mcpServerNameError(form.name);
      if (ne) {
        setError(ne);
        return;
      }
    }
    const ee = mcpEndpointError(form.endpoint);
    if (ee) {
      setError(ee);
      return;
    }
    // On edit, an authed server may reuse its stored secret (leave blank).
    const se = mcpSecretError(form.authMode, form.secret, Boolean(editing));
    if (se) {
      setError(se);
      return;
    }
    const body = {
      displayName: form.displayName || null,
      description: form.description,
      endpoint: form.endpoint.trim(),
      authMode: form.authMode,
      secret: form.authMode === "none" ? null : form.secret || null,
      trusted: form.trusted,
      enabled: form.enabled,
    };
    setBusy(true);
    try {
      const saved = editing
        ? await api.updateMcpServer(editing, body)
        : await api.createMcpServer({ name: form.name, ...body });
      await refreshMine();
      setEditing(saved.name);
      setForm(formFrom(saved));
      setNotice(
        `Connected — discovered ${saved.discoveredTools.length} tool${
          saved.discoveredTools.length === 1 ? "" : "s"
        }.`,
      );
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [editing, form, refreshMine, onChanged]);

  const test = useCallback(async () => {
    if (!editing) return;
    setError(null);
    setNotice(null);
    setTesting(true);
    try {
      // Re-supply the secret only if the user typed one; otherwise the stored
      // credential is reused server-side.
      const saved = await api.testMcpServer(editing, {
        secret: form.authMode === "none" ? null : form.secret || null,
      });
      await refreshMine();
      setForm(formFrom(saved));
      setNotice(
        `Reconnected — ${saved.discoveredTools.length} tool${
          saved.discoveredTools.length === 1 ? "" : "s"
        } available.`,
      );
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
      await refreshMine(); // pull the recorded lastError into the list/detail
    } finally {
      setTesting(false);
    }
  }, [editing, form.authMode, form.secret, refreshMine, onChanged]);

  const remove = useCallback(
    async (name: string) => {
      if (!window.confirm(`Delete MCP server "${name}"? This cannot be undone.`)) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        await api.deleteMcpServer(name);
        await refreshMine();
        if (editing === name) startNew();
        onChanged?.();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [editing, refreshMine, startNew, onChanged],
  );

  // Persist a per-tool approval override. The update path re-connects (reusing the
  // stored credential, secret left null) and prunes overrides for vanished tools,
  // mirroring the backend; a `default` choice clears the override.
  const setToolApproval = useCallback(
    async (server: UserMcpServer, toolName: string, posture: McpToolApproval) => {
      setError(null);
      setNotice(null);
      const nextApprovals: Record<string, McpToolApproval> = { ...server.toolApprovals };
      if (posture === "default") delete nextApprovals[toolName];
      else nextApprovals[toolName] = posture;
      setBusy(true);
      try {
        const saved = await api.updateMcpServer(server.name, {
          displayName: server.displayName || null,
          description: server.description,
          endpoint: server.endpoint,
          authMode: server.authMode,
          secret: null, // reuse the durably stored credential
          trusted: server.trusted,
          enabled: server.enabled,
          toolApprovals: nextApprovals,
        });
        await refreshMine();
        setForm(formFrom(saved));
        setNotice(`Approval for ${toolName} updated.`);
        onChanged?.();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [refreshMine, onChanged],
  );

  return (
    <div style={{ display: "flex", gap: 20, minHeight: 0, flex: 1 }}>
      <div style={{ width: 210, flexShrink: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <button onClick={startNew} disabled={busy} style={primaryBtn}>
          + Add MCP server
        </button>
        <ul style={{ listStyle: "none", margin: 0, padding: 0, overflowY: "auto", flex: 1 }}>
          {mine.length === 0 && (
            <li style={{ color: "var(--fg-muted)", fontSize: "0.85em", padding: 8 }}>
              No MCP servers yet.
            </li>
          )}
          {mine.map((s) => (
            <li key={s.id} style={{ display: "flex", alignItems: "center" }}>
              <button
                onClick={() => startEdit(s)}
                aria-current={editing === s.name ? "true" : undefined}
                style={{
                  flex: 1,
                  textAlign: "left",
                  padding: "8px 10px",
                  borderRadius: 8,
                  border: "none",
                  background: editing === s.name ? "var(--bg)" : "transparent",
                  color: "var(--fg)",
                  overflow: "hidden",
                }}
              >
                <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {s.lastError ? "⚠ " : ""}
                  {s.displayName || s.name}
                  {!s.enabled && <span style={{ color: "var(--fg-muted)" }}> (off)</span>}
                </span>
                <span style={{ display: "block", fontSize: "0.72em", color: "var(--fg-muted)" }}>
                  {s.trusted ? "trusted · " : ""}
                  {s.discoveredTools.length} tool{s.discoveredTools.length === 1 ? "" : "s"}
                  {(() => {
                    const b = healthBadge(s);
                    return b.tone === "ok" ? null : (
                      <span style={{ color: pillToneColor(b.tone) }}> · {b.label.toLowerCase()}</span>
                    );
                  })()}
                </span>
              </button>
              <button
                onClick={() => remove(s.name)}
                disabled={busy}
                aria-label={`Delete ${s.name}`}
                title="Delete"
                style={iconBtn}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div style={{ flex: 1, minWidth: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 14 }}>
        <h3 style={{ margin: 0, fontSize: "1em" }}>
          {editing ? `Edit ${editing}` : "Add MCP server"}
        </h3>
        <p style={{ ...labelStyle, margin: 0 }}>
          Register a remote MCP (Streamable HTTP) server you own or trust. We connect
          over a strict egress guard, list its tools, and govern each as an external
          tool whose network access is limited to the server&apos;s host.
        </p>

        <div>
          <label style={labelStyle} htmlFor="mcp-name">Name</label>
          <input
            id="mcp-name"
            value={form.name}
            disabled={!!editing}
            placeholder="e.g. weather"
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            style={{ ...inputStyle, opacity: editing ? 0.6 : 1 }}
          />
          <p style={{ ...labelStyle, marginTop: 4 }}>
            {editing
              ? "Name is the stable ID and can't be changed. To rename, add a new server and delete this one."
              : "Lowercase; the stable ID used to namespace this server's tools."}
          </p>
        </div>

        <div>
          <label style={labelStyle} htmlFor="mcp-display">Display name</label>
          <input
            id="mcp-display"
            value={form.displayName}
            maxLength={MCP_MAX_DISPLAY_NAME_LEN}
            onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="mcp-desc">Description</label>
          <input
            id="mcp-desc"
            value={form.description}
            maxLength={MCP_MAX_DESCRIPTION_LEN}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="mcp-endpoint">Endpoint URL (https)</label>
          <input
            id="mcp-endpoint"
            value={form.endpoint}
            placeholder="https://example.com/mcp"
            onChange={(e) => setForm((f) => ({ ...f, endpoint: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="mcp-auth">Authentication</label>
          <select
            id="mcp-auth"
            value={form.authMode}
            onChange={(e) =>
              setForm((f) => ({ ...f, authMode: e.target.value as McpAuthMode }))
            }
            style={inputStyle}
          >
            {MCP_AUTH_MODES.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
          <p style={{ ...labelStyle, marginTop: 4 }}>
            {MCP_AUTH_MODES.find((m) => m.value === form.authMode)?.hint}
          </p>
        </div>

        {form.authMode !== "none" && (
          <div>
            <label style={labelStyle} htmlFor="mcp-secret">
              {form.authMode === "api_key" ? "API key" : "Bearer token"}
            </label>
            <input
              id="mcp-secret"
              type="password"
              value={form.secret}
              autoComplete="off"
              placeholder={editing ? "Leave blank to keep the stored secret" : ""}
              onChange={(e) => setForm((f) => ({ ...f, secret: e.target.value }))}
              style={inputStyle}
            />
            <p style={{ ...labelStyle, marginTop: 4 }}>
              Stored encrypted; the server never returns it. We use it only to connect.
            </p>
          </div>
        )}

        <label style={checkRow}>
          <input
            type="checkbox"
            checked={form.trusted}
            onChange={(e) => setForm((f) => ({ ...f, trusted: e.target.checked }))}
          />
          Trusted — its default-posture tools become callable
        </label>
        <p style={{ ...labelStyle, marginTop: -8 }}>
          Chat has no live approval prompt: leave this off (recommended) and
          tools without a Never override stay unavailable, not merely
          slower. Only trust a server you fully control, or pre-approve
          individual tools below. Trusting it won&apos;t help while the
          server is disabled or quarantined, though — either blocks every
          tool regardless.
        </p>

        <label style={checkRow}>
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
          />
          Enabled
        </label>

        {error && (
          <p role="alert" style={{ color: "var(--danger)", fontSize: "0.85em", margin: 0 }}>
            {error}
          </p>
        )}
        {notice && (
          <p style={{ color: "var(--success)", fontSize: "0.85em", margin: 0 }}>{notice}</p>
        )}

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button onClick={submit} disabled={busy || testing} style={primaryBtn}>
            {busy ? "Connecting…" : editing ? "Save & reconnect" : "Connect & save"}
          </button>
          {editing && (
            <button onClick={test} disabled={busy || testing} style={secondaryBtn}>
              {testing ? "Testing…" : "Test"}
            </button>
          )}
        </div>

        {current && (
          <DiscoverySection
            server={current}
            busy={busy || testing}
            onSetToolApproval={setToolApproval}
          />
        )}
      </div>
    </div>
  );
}

// Discovery results + governance posture for a saved server, so the user can see
// exactly which tools they'll be able to attach, how each is governed, and the
// server's health/quarantine state. Per-tool approval can be set inline.
function DiscoverySection({
  server,
  busy,
  onSetToolApproval,
}: {
  server: UserMcpServer;
  busy: boolean;
  onSetToolApproval: (
    server: UserMcpServer,
    toolName: string,
    posture: McpToolApproval,
  ) => void;
}) {
  const health = healthBadge(server);
  const quarantineMsg = quarantineReason(server);
  const quarantined = health.status === "quarantined";
  const posture = approvalPosture({
    trusted: server.trusted,
    host: server.host,
    blocking: !server.enabled ? "disabled" : quarantined ? "quarantined" : null,
  });
  return (
    <div style={fieldset}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <strong style={{ fontSize: "0.85em" }}>
          Discovered tools ({server.discoveredTools.length})
        </strong>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
          <Pill
            label={health.label}
            tone={health.tone}
            detail={health.detail}
            helpLabel={`Health: ${health.label}`}
          />
          {/* No `detail` here: the persistent paragraph just below already shows
              the same text, so a redundant tooltip would add noise, not clarity. */}
          <Pill label={posture.label} tone={posture.requiresApproval ? "muted" : "ok"} />
        </div>
      </div>
      <p style={{ ...labelStyle, margin: 0 }}>{posture.detail}</p>

      {quarantineMsg && (
        <p role="alert" style={{ color: "var(--danger)", fontSize: "0.8em", margin: "4px 0 0" }}>
          {quarantineMsg}
        </p>
      )}

      {server.lastError && (
        <p role="alert" style={{ color: "var(--danger)", fontSize: "0.8em", margin: "4px 0 0" }}>
          Last connection error: {server.lastError}
        </p>
      )}

      {server.discoveredTools.length === 0 ? (
        <p style={{ ...labelStyle, margin: 0 }}>
          No tools discovered. Use Test to reconnect once the server advertises tools.
        </p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {server.discoveredTools.map((t, ti) => {
            const tp = toolApprovalPosture(server, t.name, quarantined);
            const approvalOption = MCP_TOOL_APPROVALS.find((a) => a.value === tp.posture);
            // Index-based, not name-derived: a discovered tool name can contain
            // whitespace or (in principle) repeat, so it isn't a safe/unique id.
            const approvalSelectId = `approval-${server.name}-${ti}`;
            return (
              <li key={t.name} style={{ fontSize: "0.82em", display: "flex", flexDirection: "column", gap: 4 }}>
                <div>
                  <code style={{ color: "var(--fg)" }}>{t.name}</code>
                  {t.description && (
                    <span style={{ color: "var(--fg-muted)" }}> — {t.description}</span>
                  )}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <label style={{ ...labelStyle, margin: 0 }} htmlFor={approvalSelectId}>
                    Approval
                  </label>
                  <select
                    id={approvalSelectId}
                    value={tp.posture}
                    disabled={busy}
                    onChange={(e) =>
                      onSetToolApproval(server, t.name, e.target.value as McpToolApproval)
                    }
                    style={{ ...inputStyle, width: "auto", padding: "4px 8px", fontSize: "0.82em" }}
                  >
                    {MCP_TOOL_APPROVALS.map((a) => (
                      <option key={a.value} value={a.value} title={a.hint}>{a.label}</option>
                    ))}
                  </select>
                  <HelpTooltip
                    label={`Approval option: ${approvalOption?.label ?? tp.posture}`}
                    size="sm"
                  >
                    {approvalOption?.hint}
                  </HelpTooltip>
                  <Pill
                    label={tp.label}
                    tone={tp.requiresApproval ? "muted" : "ok"}
                    detail={tp.detail}
                    helpLabel={`Resolved approval for ${t.name}`}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <p style={{ ...labelStyle, margin: 0 }}>
        Attach these tools to an agent from the Agents tab.
      </p>
    </div>
  );
}
