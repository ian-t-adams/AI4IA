"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/api";
import {
  approvalPosture,
  healthBadge,
  MCP_AUTH_MODES,
  MCP_MAX_DESCRIPTION_LEN,
  MCP_MAX_DISPLAY_NAME_LEN,
  MCP_PROTOCOL_VERSIONS,
  MCP_TOOL_APPROVALS,
  mcpEndpointError,
  mcpSecretError,
  mcpServerNameError,
  quarantineReason,
  toolApprovalPosture,
  type McpAuthMode,
  type McpProtocolVersion,
  type McpToolApproval,
  type UserMcpServer,
} from "@/lib/customTools";
import { HelpTooltip } from "./HelpTooltip";
import { Pill, pillToneColor } from "./Pill";
import { suggestedStudioName } from "@/lib/studio";
import { useStudioDraft } from "./useStudioDraft";
import {
  checkRow,
  fieldset,
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
  protocolVersion: McpProtocolVersion;
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
    protocolVersion: "2025-06-18",
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
    protocolVersion: s.protocolVersion ?? "2025-06-18",
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
  const [savedForm, setSavedForm] = useState<ServerForm>(blankForm);
  const [loading, setLoading] = useState(true);
  const [listUnavailable, setListUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(savedForm);
  const confirmDiscard = useStudioDraft(dirty, busy || testing);
  const newName = form.name || suggestedStudioName(form.displayName, mine.map((s) => s.name), "tools");

  const current = editing ? mine.find((s) => s.name === editing) ?? null : null;

  const refreshMine = useCallback(async () => {
    try {
      setMine(await api.listMcpServers());
      setListUnavailable(false);
    } catch (e) {
      setListUnavailable(true);
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refreshMine();
  }, [refreshMine]);

  const startNew = useCallback(() => {
    setEditing(null);
    setForm(blankForm());
    setSavedForm(blankForm());
    setError(null);
    setNotice(null);
  }, []);

  const startEdit = useCallback((s: UserMcpServer) => {
    setEditing(s.name);
    setForm(formFrom(s));
    setSavedForm(formFrom(s));
    setError(null);
    setNotice(null);
  }, []);

  const submit = useCallback(async () => {
    setError(null);
    setNotice(null);
    if (!editing) {
      if (!form.displayName.trim()) {
        setError("Connection name is required.");
        return;
      }
      const ne = mcpServerNameError(newName);
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
      protocolVersion: form.protocolVersion,
      authMode: form.authMode,
      secret: form.authMode === "none" ? null : form.secret || null,
      trusted: form.trusted,
      enabled: form.enabled,
    };
    setBusy(true);
    try {
      const saved = editing
        ? await api.updateMcpServer(editing, body)
        : await api.createMcpServer({ name: newName, ...body });
      await refreshMine();
      setEditing(saved.name);
      setForm(formFrom(saved));
      setSavedForm(formFrom(saved));
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
  }, [editing, form, newName, refreshMine, onChanged]);

  const test = useCallback(async () => {
    if (!editing || dirty) return;
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
      setSavedForm(formFrom(saved));
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
  }, [editing, dirty, form.authMode, form.secret, refreshMine, onChanged]);

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
      if (dirty) return;
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
          protocolVersion: server.protocolVersion ?? "2025-06-18",
          authMode: server.authMode,
          secret: null, // reuse the durably stored credential
          trusted: server.trusted,
          enabled: server.enabled,
          toolApprovals: nextApprovals,
        });
        await refreshMine();
        setForm(formFrom(saved));
        setSavedForm(formFrom(saved));
        setNotice(`Approval for ${toolName} updated.`);
        onChanged?.();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [dirty, refreshMine, onChanged],
  );

  return (
    <div className="studio-pane">
      <div className="studio-list">
        <button onClick={() => { if (confirmDiscard()) startNew(); }} disabled={busy || testing || loading} style={secondaryBtn}>
          New connection
        </button>
        <h3 className="studio-list-heading">Your connections</h3>
        <ul className="studio-list-scroll">
          {loading && <li role="status">Loading your connections...</li>}
          {listUnavailable && <li role="status">Your connections could not be loaded. <button onClick={() => { setError(null); void refreshMine(); }} style={secondaryBtn}>Retry</button></li>}
          {!loading && !listUnavailable && mine.length === 0 && (
            <li style={{ color: "var(--fg-muted)", fontSize: "0.85em", padding: 8 }}>
              Connect an existing server to make its tools available here.
            </li>
          )}
          {mine.map((s) => (
            <li key={s.id} className="studio-list-row">
              <button
                className="studio-list-select"
                disabled={busy || testing}
                onClick={() => { if (editing !== s.name && confirmDiscard()) startEdit(s); }}
                aria-current={editing === s.name ? "true" : undefined}
              >
                <span style={{ display: "block" }}>
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
                className="studio-list-delete"
                onClick={() => remove(s.name)}
                disabled={busy || testing}
                aria-label={`Delete ${s.name}`}
                title="Delete"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      </div>

      <fieldset className="studio-workpanel studio-form" disabled={busy || testing}>
        <div className="studio-editor-header">
          <div>
            <h3>{editing ? `Edit ${current?.displayName || editing}` : "Connect custom tools"}</h3>
            <p className="studio-help">Connect an existing MCP server over Streamable HTTP, then attach its tools to an agent.</p>
          </div>
          <button onClick={submit} disabled={busy || testing || loading} style={primaryBtn}>
            {busy ? "Connecting…" : editing ? "Save & reconnect" : "Connect & save"}
          </button>
          {error && <p role="alert" className="studio-alert">{error}</p>}
          <p role="status" className="studio-save-status">{dirty ? "Unsaved changes" : notice}</p>
        </div>

        <div>
          <label style={labelStyle} htmlFor="mcp-display">Connection name</label>
          <input
            id="mcp-display"
            value={form.displayName}
            maxLength={MCP_MAX_DISPLAY_NAME_LEN}
            placeholder="e.g. Company knowledge"
            onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
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
          Trusted for discovery — default-posture tools become attachable
        </label>
        <p style={{ ...labelStyle, marginTop: -8 }}>
          Leave this off unless you control the server. Trust changes discovery
          and attachment only. Invocation is governed separately; the default
          interactive policy holds external or destructive calls for exact-call
          approval. A disabled or quarantined server blocks every tool regardless.
        </p>

        <label style={checkRow}>
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
          />
          Enabled
        </label>

        <details className="studio-details">
          <summary>Connection settings <span>Protocol, description and ID</span></summary>
          <div>
            <label style={labelStyle} htmlFor="mcp-protocol">MCP protocol</label>
            <select id="mcp-protocol" value={form.protocolVersion} style={inputStyle}
              onChange={(e) => {
                const choice = MCP_PROTOCOL_VERSIONS.find((p) => p.value === e.target.value);
                if (!choice) { setError("Unsupported MCP protocol."); return; }
                setForm((f) => ({ ...f, protocolVersion: choice.value }));
              }}>
              {MCP_PROTOCOL_VERSIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
            <p className="studio-help">Change versions only after verifying server support. Saving reconnects and changes the tool consent contract. Errors never switch protocols or replay calls.</p>
          </div>
          <div>
            <label style={labelStyle} htmlFor="mcp-desc">Description</label>
            <input id="mcp-desc" value={form.description} maxLength={MCP_MAX_DESCRIPTION_LEN}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} style={inputStyle} />
          </div>
          <div>
            <label style={labelStyle} htmlFor="mcp-name">Connection ID</label>
            <input id="mcp-name" value={editing ? form.name : newName} disabled={!!editing}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} style={inputStyle} />
            <p className="studio-help">Suggested from the name for new connections. Saved IDs keep agent tool references intact and cannot change.</p>
          </div>
        </details>

        <div className="studio-inline-actions">
          {editing && (
            <button onClick={test} disabled={busy || testing || dirty} style={secondaryBtn}>
              {testing ? "Testing…" : "Test"}
            </button>
          )}
          {editing && dirty && <p className="studio-help">Save changes before testing or changing tool approvals.</p>}
        </div>

        {current && (
          <DiscoverySection
            server={current}
            busy={busy || testing || dirty}
            onSetToolApproval={setToolApproval}
          />
        )}
      </fieldset>
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
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
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
