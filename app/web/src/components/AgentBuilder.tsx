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

const labelStyle: React.CSSProperties = {
  fontSize: "0.8em",
  color: "var(--fg-muted)",
  marginBottom: 4,
  display: "block",
};
const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "8px 10px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  font: "inherit",
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
  onChanged,
}: {
  agents: AgentSummary[];
  models: ModelEntry[];
  onChanged: () => Promise<void>;
}) {
  const [mine, setMine] = useState<UserAgent[]>([]);
  const [editing, setEditing] = useState<string | null>(null); // name, or null = new
  const [form, setForm] = useState<AgentForm>(blankForm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chatModels = useMemo(
    () => models.filter((m) => m.category === "chat"),
    [models],
  );
  const linkOptions = useMemo(
    () => agents.filter((a) => a.name !== form.name),
    [agents, form.name],
  );

  const refreshMine = useCallback(async () => {
    try {
      setMine(await api.listMyAgents());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refreshMine();
  }, [refreshMine]);

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
          {ATTACHABLE_TOOLS.map((t) => (
            <label key={t} style={checkRow}>
              <input
                type="checkbox"
                checked={form.tools.includes(t)}
                onChange={() => toggleIn("tools", t)}
              />
              {t}
            </label>
          ))}
        </fieldset>

        <fieldset style={fieldset}>
          <legend style={labelStyle}>Delegate to (links, max {MAX_LINKS})</legend>
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

const primaryBtn: React.CSSProperties = {
  padding: "9px 16px",
  borderRadius: 8,
  border: "none",
  background: "var(--accent)",
  color: "var(--accent-fg)",
  fontWeight: 600,
  cursor: "pointer",
};
const iconBtn: React.CSSProperties = {
  border: "none",
  background: "transparent",
  color: "var(--fg-muted)",
  padding: "4px 6px",
  cursor: "pointer",
};
const fieldset: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 8,
  margin: 0,
  padding: "8px 12px",
  display: "flex",
  flexDirection: "column",
  gap: 4,
};
const checkRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontSize: "0.9em",
};
