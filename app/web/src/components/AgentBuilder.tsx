"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "@/lib/api";
import type { AgentSummary, ModelEntry, UserAgent } from "@/lib/types";
import {
  ATTACHABLE_TOOLS,
  MAX_DESCRIPTION_LEN,
  MAX_DISPLAY_NAME_LEN,
  MAX_LINKS,
  MAX_SYSTEM_PROMPT_LEN,
  MAX_TOOLS,
  nameError,
} from "@/lib/studio";
import {
  approvalPosture,
  attachableMcpTools,
  healthBadge,
  isMcpToolName,
  parseMcpToolName,
  quarantineReason,
  type AttachableMcpTool,
  type UserMcpServer,
} from "@/lib/customTools";
import { BUILT_IN_TOOL_HELP, toolRiskSummary } from "@/lib/toolHelp";
import { HelpTooltip } from "./HelpTooltip";
import { Pill } from "./Pill";
import {
  checkRow,
  fieldset,
  iconBtn,
  inputStyle,
  labelStyle,
  primaryBtn,
} from "./builderStyles";

// Friendly names for the attachable tools (the registry uses snake_case ids).
const TOOL_LABELS: Record<string, string> = {
  calculator: "Calculator",
  get_current_time: "Current time",
  generate_image: "Generate image",
  generate_video: "Generate video",
  process_document: "Process document",
  recall_memory: "Recall memory",
};

interface AgentForm {
  name: string;
  displayName: string;
  description: string;
  systemPrompt: string;
  defaultModel: string; // "" = session default
  tools: string[];
  links: string[];
  enabled: boolean;
}

function blankForm(): AgentForm {
  return {
    name: "",
    displayName: "",
    description: "",
    systemPrompt: "",
    defaultModel: "",
    tools: [],
    links: [],
    enabled: true,
  };
}

function formFrom(a: UserAgent): AgentForm {
  return {
    name: a.name,
    displayName: a.displayName,
    description: a.description,
    systemPrompt: a.systemPrompt,
    defaultModel: a.defaultModel ?? "",
    tools: [...a.tools],
    links: [...a.links],
    enabled: a.enabled,
  };
}

export function AgentBuilder({
  agents,
  models,
  customToolsEnabled = false,
  onChanged,
}: {
  agents: AgentSummary[];
  models: ModelEntry[];
  customToolsEnabled?: boolean;
  onChanged: () => Promise<void>;
}) {
  const [mine, setMine] = useState<UserAgent[]>([]);
  const [mcpServers, setMcpServers] = useState<UserMcpServer[]>([]);
  const [officialMcpServers, setOfficialMcpServers] = useState<UserMcpServer[]>(
    [],
  );
  const [editing, setEditing] = useState<string | null>(null); // name, or null = new
  const [form, setForm] = useState<AgentForm>(blankForm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chatModels = useMemo(
    () => models.filter((m) => m.conversational),
    [models],
  );
  const linkOptions = useMemo(
    () => agents.filter((a) => a.name !== form.name),
    [agents, form.name],
  );
  // Names of the signed-in user's own agents, so the "Delegate to" list can
  // distinguish an agent you authored from a pre-created one shared by an
  // administrator (both are valid delegation targets, but only "yours" can be
  // edited/deleted from this picker).
  const myAgentNames = useMemo(() => new Set(mine.map((m) => m.name)), [mine]);
  // Attachable MCP tools grouped by server (only when the feature is on). Each
  // carries its namespaced name + governance posture so the user understands the
  // stance before attaching.
  // Official (curated, APIM-fronted) tools first, then the caller's BYO tools.
  // Official servers win a name collision (mirrors the backend merge), and BYO is
  // only surfaced when the custom-tools feature is on; the official plane stands
  // on its own otherwise. Each carries its namespaced name + governance posture so
  // the user understands the stance before attaching.
  const mcpTools = useMemo(() => {
    const official = attachableMcpTools(officialMcpServers, { official: true });
    const officialNames = new Set(officialMcpServers.map((s) => s.name));
    const byo = customToolsEnabled
      ? attachableMcpTools(mcpServers.filter((s) => !officialNames.has(s.name)))
      : [];
    return [...official, ...byo];
  }, [officialMcpServers, mcpServers, customToolsEnabled]);
  const mcpByServer = useMemo(() => groupByServer(mcpTools), [mcpTools]);
  // Server records keyed by name so a group can surface its health/quarantine badge.
  // Official records override BYO on a name clash so the surviving group's health shows.
  const mcpServerByName = useMemo(
    () => new Map([...mcpServers, ...officialMcpServers].map((s) => [s.name, s])),
    [mcpServers, officialMcpServers],
  );
  // MCP tools still attached to this agent whose server/tool no longer exists, so
  // the user can detach them even though there's no checkbox to render otherwise.
  const orphanMcpTools = useMemo(() => {
    const known = new Set(mcpTools.map((t) => t.namespacedName));
    return form.tools.filter((t) => isMcpToolName(t) && !known.has(t));
  }, [form.tools, mcpTools]);

  const refreshMine = useCallback(async () => {
    try {
      setMine(await api.listMyAgents());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const refreshMcp = useCallback(async () => {
    if (!customToolsEnabled) return;
    try {
      setMcpServers(await api.listMcpServers());
    } catch {
      // A custom-tools store blip must never break agent editing; just show no
      // MCP tools to attach (the built-in tools still work).
      setMcpServers([]);
    }
  }, [customToolsEnabled]);

  const refreshOfficialMcp = useCallback(async () => {
    try {
      setOfficialMcpServers(await api.listOfficialMcpServers());
    } catch {
      // The official endpoint returns [] when the plane is off; treat any blip the
      // same so agent editing is never blocked by it.
      setOfficialMcpServers([]);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refreshMine();
  }, [refreshMine]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch that also re-runs when `customToolsEnabled` flips; kept separate from the other refreshes so toggling it doesn't re-fetch unrelated data
    void refreshMcp();
  }, [refreshMcp]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refreshOfficialMcp();
  }, [refreshOfficialMcp]);

  const startNew = useCallback(() => {
    setEditing(null);
    setForm(blankForm());
    setError(null);
  }, []);

  const startEdit = useCallback((a: UserAgent) => {
    setEditing(a.name);
    setForm(formFrom(a));
    setError(null);
  }, []);

  const toggleIn = useCallback(
    (key: "tools" | "links", value: string) =>
      setForm((f) => {
        const has = f[key].includes(value);
        return { ...f, [key]: has ? f[key].filter((x) => x !== value) : [...f[key], value] };
      }),
    [],
  );

  const submit = useCallback(async () => {
    setError(null);
    if (!editing) {
      const ne = nameError(form.name);
      if (ne) {
        setError(ne);
        return;
      }
    }
    if (!form.systemPrompt.trim()) {
      setError("System prompt is required.");
      return;
    }
    const body = {
      displayName: form.displayName || null,
      description: form.description,
      systemPrompt: form.systemPrompt,
      defaultModel: form.defaultModel || null,
      tools: form.tools,
      links: form.links,
      enabled: form.enabled,
    };
    setBusy(true);
    try {
      const saved = editing
        ? await api.updateAgent(editing, body)
        : await api.createAgent({ name: form.name, ...body });
      await refreshMine();
      await onChanged(); // keep the @-mention menu + link pickers fresh
      setEditing(saved.name);
      setForm(formFrom(saved)); // reflect server-sanitized links
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [editing, form, refreshMine, onChanged]);

  const remove = useCallback(
    async (name: string) => {
      setBusy(true);
      setError(null);
      try {
        await api.deleteAgent(name);
        await refreshMine();
        await onChanged();
        if (editing === name) startNew();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [editing, refreshMine, onChanged, startNew],
  );

  return (
    <div style={{ display: "flex", gap: 20, minHeight: 0, flex: 1 }}>
      <div style={{ width: 200, flexShrink: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <button onClick={startNew} disabled={busy} style={primaryBtn}>
          + New agent
        </button>
        <ul style={{ listStyle: "none", margin: 0, padding: 0, overflowY: "auto", flex: 1 }}>
          {mine.length === 0 && (
            <li style={{ color: "var(--fg-muted)", fontSize: "0.85em", padding: 8 }}>
              No agents yet.
            </li>
          )}
          {mine.map((a) => (
            <li key={a.id} style={{ display: "flex", alignItems: "center" }}>
              <button
                onClick={() => startEdit(a)}
                aria-current={editing === a.name ? "true" : undefined}
                style={{
                  flex: 1,
                  textAlign: "left",
                  padding: "8px 10px",
                  borderRadius: 8,
                  border: "none",
                  background: editing === a.name ? "var(--bg)" : "transparent",
                  color: "var(--fg)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {a.displayName || a.name}
                {!a.enabled && <span style={{ color: "var(--fg-muted)" }}> (off)</span>}
              </button>
              <button
                onClick={() => remove(a.name)}
                disabled={busy}
                aria-label={`Delete ${a.name}`}
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
          {editing ? `Edit @${editing}` : "New agent"}
        </h3>

        <div>
          <label style={labelStyle} htmlFor="ag-name">Name (@mention)</label>
          <input
            id="ag-name"
            value={form.name}
            disabled={!!editing}
            placeholder="e.g. pirate"
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            style={{ ...inputStyle, opacity: editing ? 0.6 : 1 }}
          />
          <p style={{ ...labelStyle, marginTop: 4 }}>
            {editing
              ? "Name is the stable @mention ID and can't be changed. To rename, create a new agent and delete this one."
              : "Lowercase; the stable ID used by @mentions and workflows."}
          </p>
        </div>

        <div>
          <label style={labelStyle} htmlFor="ag-display">Display name</label>
          <input
            id="ag-display"
            value={form.displayName}
            maxLength={MAX_DISPLAY_NAME_LEN}
            onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="ag-desc">Description</label>
          <input
            id="ag-desc"
            value={form.description}
            maxLength={MAX_DESCRIPTION_LEN}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            style={inputStyle}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="ag-sys">System prompt</label>
          <textarea
            id="ag-sys"
            value={form.systemPrompt}
            maxLength={MAX_SYSTEM_PROMPT_LEN}
            rows={5}
            onChange={(e) => setForm((f) => ({ ...f, systemPrompt: e.target.value }))}
            style={{ ...inputStyle, resize: "vertical" }}
          />
        </div>

        <div>
          <label style={labelStyle} htmlFor="ag-model">Preferred model</label>
          <select
            id="ag-model"
            value={form.defaultModel}
            onChange={(e) => setForm((f) => ({ ...f, defaultModel: e.target.value }))}
            style={inputStyle}
          >
            <option value="">Session default</option>
            {chatModels.map((m) => (
              <option key={m.id} value={m.id}>{m.displayName}</option>
            ))}
          </select>
        </div>

        <fieldset style={fieldset}>
          <legend style={labelStyle}>Tools (max {MAX_TOOLS})</legend>
          {ATTACHABLE_TOOLS.map((t) => {
            const help = BUILT_IN_TOOL_HELP[t];
            const label = TOOL_LABELS[t] ?? t;
            return (
              <label key={t} style={checkRow}>
                <input
                  type="checkbox"
                  checked={form.tools.includes(t)}
                  onChange={() => toggleIn("tools", t)}
                />
                {label}
                {help && (
                  <HelpTooltip label={label} size="sm">
                    {help.what} {help.when} {help.tradeoffs}{" "}
                    {toolRiskSummary(help.risk)}
                  </HelpTooltip>
                )}
              </label>
            );
          })}
        </fieldset>

        {(customToolsEnabled || mcpByServer.length > 0) && (
          <fieldset style={fieldset}>
            <legend style={labelStyle}>MCP tools</legend>
            {mcpByServer.length === 0 && orphanMcpTools.length === 0 && (
              <p style={{ ...labelStyle, margin: 0 }}>
                No MCP tools yet. Register a server in the Custom tools tab, then its
                tools appear here to attach.
              </p>
            )}
            {mcpByServer.map((g) => {
              const posture = approvalPosture({ trusted: g.trusted, host: g.host });
              const serverRec = mcpServerByName.get(g.serverName);
              const health = serverRec ? healthBadge(serverRec) : null;
              const quarantineMsg = serverRec ? quarantineReason(serverRec) : null;
              return (
                <div key={g.serverName} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <strong style={{ fontSize: "0.82em" }}>
                      {g.serverDisplayName}
                      {!g.enabled && <span style={{ color: "var(--fg-muted)" }}> (server off)</span>}
                    </strong>
                    {health && health.tone !== "ok" && (
                      <Pill
                        label={health.label}
                        tone={health.tone}
                        detail={health.detail}
                        helpLabel={`Health: ${health.label}`}
                      />
                    )}
                    {g.official ? (
                      <Pill
                        label="official · curated"
                        tone="ok"
                        detail="Curated official server, reached through the MCP APIM front door and managed by your administrator. Its tools are pre-approved."
                        helpLabel="Official server"
                      />
                    ) : (
                      <Pill
                        label={posture.label}
                        tone={posture.requiresApproval ? "muted" : "ok"}
                        detail={posture.detail}
                        helpLabel="Approval posture"
                      />
                    )}
                  </div>
                  {quarantineMsg && (
                    <p role="alert" style={{ color: "var(--danger)", fontSize: "0.75em", margin: 0 }}>
                      {quarantineMsg}
                    </p>
                  )}
                  {g.tools.map((t) => (
                    <label key={t.namespacedName} style={checkRow}>
                      <input
                        type="checkbox"
                        checked={form.tools.includes(t.namespacedName)}
                        onChange={() => toggleIn("tools", t.namespacedName)}
                      />
                      {t.toolName}
                      {t.description && (
                        <HelpTooltip label={t.toolName} size="sm">
                          {t.description}
                        </HelpTooltip>
                      )}
                      <span
                        style={{
                          fontSize: "0.72em",
                          color: t.requiresApproval ? "var(--fg-muted)" : "#15803d",
                        }}
                      >
                        {t.requiresApproval
                          ? t.approval === "always"
                            ? "· approval (forced)"
                            : "· approval"
                          : t.approval === "never"
                            ? "· pre-approved"
                            : "· auto"}
                      </span>
                    </label>
                  ))}
                </div>
              );
            })}
            {orphanMcpTools.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <strong style={{ fontSize: "0.82em", color: "var(--fg-muted)" }}>
                  Unavailable (server removed)
                </strong>
                {orphanMcpTools.map((name) => {
                  const parsed = parseMcpToolName(name);
                  return (
                    <label key={name} style={{ ...checkRow, color: "var(--fg-muted)" }}>
                      <input
                        type="checkbox"
                        checked
                        onChange={() => toggleIn("tools", name)}
                      />
                      {parsed ? `${parsed.server} / ${parsed.tool}` : name}
                    </label>
                  );
                })}
              </div>
            )}
          </fieldset>
        )}

        <fieldset style={fieldset}>
          <legend style={labelStyle}>
            Delegate to (links, max {MAX_LINKS}){" "}
            <HelpTooltip label="Delegate to (links)" size="sm">
              Lets this agent hand off part of a conversation to another agent by name (e.g.
              &ldquo;ask the Research agent to&hellip;&rdquo;). Linking an agent only makes it
              available to delegate to &mdash; it does not run automatically and does not give
              this agent the linked agent&apos;s tools directly.
            </HelpTooltip>
          </legend>
          {linkOptions.length === 0 && (
            <p style={{ ...labelStyle, margin: 0 }}>No other agents to link.</p>
          )}
          {linkOptions.map((a) => (
            <label key={a.name} style={checkRow}>
              <input
                type="checkbox"
                checked={form.links.includes(a.name)}
                onChange={() => toggleIn("links", a.name)}
              />
              {a.displayName || a.name}
              <span style={{ fontSize: "0.72em", color: "var(--fg-muted)" }}>
                {myAgentNames.has(a.name) ? "· yours" : "· pre-created"}
              </span>
              {a.description && (
                <HelpTooltip label={a.displayName || a.name} size="sm">
                  {a.description}
                </HelpTooltip>
              )}
            </label>
          ))}
        </fieldset>

        <label style={{ ...checkRow, fontSize: "0.9em" }}>
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

        <div>
          <button onClick={submit} disabled={busy} style={primaryBtn}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create agent"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface McpServerGroup {
  serverName: string;
  serverDisplayName: string;
  trusted: boolean;
  enabled: boolean;
  host: string;
  official: boolean;
  tools: AttachableMcpTool[];
}

// Groups flat attachable MCP tools by their server, preserving first-seen order so
// the builder can render one posture badge + a tool checklist per server.
function groupByServer(tools: AttachableMcpTool[]): McpServerGroup[] {
  const order: string[] = [];
  const byName = new Map<string, McpServerGroup>();
  for (const t of tools) {
    let g = byName.get(t.serverName);
    if (!g) {
      g = {
        serverName: t.serverName,
        serverDisplayName: t.serverDisplayName,
        trusted: t.trusted,
        enabled: t.enabled,
        host: t.host,
        official: t.official,
        tools: [],
      };
      byName.set(t.serverName, g);
      order.push(t.serverName);
    }
    g.tools.push(t);
  }
  return order.map((n) => byName.get(n)!);
}

