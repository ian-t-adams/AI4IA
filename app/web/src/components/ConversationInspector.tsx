"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import * as api from "@/lib/api";
import {
  deleteMemory,
  getInspector,
  getLibrarySummary,
  listMemories,
  type InspectorSnapshot,
  type LibrarySummary,
  type MemoryList,
} from "@/lib/inspector";
import type {
  AgentSummary,
  ChatParams,
  ModelEntry,
  Session,
  ToolCatalogItem,
} from "@/lib/types";
import { HelpTooltip } from "./HelpTooltip";
import { ModelPicker } from "./ModelPicker";
import { ParamControls } from "./ParamControls";
import { VoiceSettingsPanel, type VoiceSettingsPanelProps } from "./VoiceSettingsPanel";
import { useMediaQuery } from "./useMediaQuery";
import { useModalFocus } from "./useModalFocus";

type Section = "model" | "instructions" | "tools" | "context" | "memory" | "usage" | "voice";

function money(microUsd: number): string {
  return `$${(microUsd / 1_000_000).toFixed(4)}`;
}

function SectionTitle({
  title,
  help,
}: {
  title: string;
  help: React.ReactNode;
}) {
  return (
    <div className="inspector-section-title">
      <h2>{title}</h2>
      <HelpTooltip label={title}>{help}</HelpTooltip>
    </div>
  );
}

export function ConversationInspector({
  sessionId,
  models,
  agents,
  selectedModel,
  onModelChange,
  params,
  onParamsChange,
  systemPrompt,
  onSystemPromptChange,
  onSessionUpdated,
  onOpenLibrary,
  voiceSettings,
  voiceLocked,
  collapsed,
  onToggle,
}: {
  sessionId: string | null;
  models: ModelEntry[];
  agents: AgentSummary[];
  selectedModel: string | null;
  onModelChange: (model: string) => void;
  params: ChatParams;
  onParamsChange: (params: ChatParams) => void;
  systemPrompt: string;
  onSystemPromptChange: (prompt: string) => Promise<void>;
  onSessionUpdated: (session: Session) => void;
  onOpenLibrary?: () => void;
  voiceSettings?: Omit<VoiceSettingsPanelProps, "locked">;
  voiceLocked: boolean;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const [section, setSection] = useState<Section>("model");
  const [snapshot, setSnapshot] = useState<InspectorSnapshot | null>(null);
  const [tools, setTools] = useState<ToolCatalogItem[]>([]);
  const [memory, setMemory] = useState<MemoryList | null>(null);
  const [library, setLibrary] = useState<LibrarySummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [promptDraft, setPromptDraft] = useState(systemPrompt);
  const drawer = useMediaQuery("(max-width: 1050px)") && !collapsed;
  const drawerFocus = useModalFocus(onToggle, drawer);

  useEffect(() => setPromptDraft(systemPrompt), [systemPrompt]);

  const load = useCallback(async () => {
    if (!sessionId) {
      setSnapshot(null);
      return;
    }
    setLoading(true);
    setError(null);
    const [inspector, toolList, memories, librarySummary] = await Promise.allSettled([
      getInspector(sessionId),
      api.listTools(),
      listMemories(),
      getLibrarySummary(),
    ]);
    if (inspector.status === "fulfilled") setSnapshot(inspector.value);
    else setError(inspector.reason instanceof Error ? inspector.reason.message : "Inspector unavailable.");
    if (toolList.status === "fulfilled") setTools(toolList.value);
    if (memories.status === "fulfilled") setMemory(memories.value);
    if (librarySummary.status === "fulfilled") setLibrary(librarySummary.value);
    setLoading(false);
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = useCallback(
    async (value: Parameters<typeof api.updateSession>[1]) => {
      if (!sessionId) return;
      try {
        const updated = await api.updateSession(sessionId, value);
        onSessionUpdated(updated);
        await load();
      } catch (reason) {
        setError((reason as Error).message);
      }
    },
    [load, onSessionUpdated, sessionId],
  );

  const selectedIds = useMemo(
    () => new Set(snapshot?.libraryDocuments.map((document) => document.id) ?? []),
    [snapshot],
  );

  if (collapsed) {
    return (
      <aside className="conversation-inspector collapsed" aria-label="Conversation inspector">
        <button type="button" onClick={onToggle} aria-label="Open conversation inspector">
          ‹
        </button>
      </aside>
    );
  }

  return (
    <aside
      ref={drawerFocus.ref}
      onKeyDown={drawerFocus.onKeyDown}
      className="conversation-inspector"
      aria-label="Conversation inspector"
      aria-busy={loading}
    >
      <header className="inspector-header">
        <div>
          <strong>Conversation</strong>
          <span aria-live="polite">
            {loading ? "Updating…" : snapshot ? "Inspector" : "Defaults"}
          </span>
        </div>
        <button type="button" onClick={onToggle} aria-label="Collapse conversation inspector">
          ›
        </button>
      </header>

      <div className="inspector-nav" role="tablist" aria-label="Inspector sections">
        {(
          [
            ["model", "Model"],
            ["instructions", "Instructions"],
            ["tools", "Agent & tools"],
            ["context", "Context"],
            ["memory", "Memory"],
            ["usage", "Usage"],
            ["voice", "Voice"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            id={`inspector-tab-${id}`}
            role="tab"
            aria-selected={section === id}
            aria-controls={`inspector-panel-${id}`}
            onClick={() => setSection(id)}
          >
            {label}
          </button>
        ))}
      </div>

      {error ? <div className="inspector-error" role="alert">{error}</div> : null}

      <div
        id={`inspector-panel-${section}`}
        role="tabpanel"
        aria-labelledby={`inspector-tab-${section}`}
        className="inspector-body"
      >
        {section === "model" ? (
          <section>
            <SectionTitle
              title="Model"
              help="Selects the catalog model for this chat. Temperature changes variety, top-p limits the probability mass sampled, and max tokens caps output. Availability and ceilings come from the server catalog."
            />
            <ModelPicker models={models} value={selectedModel} onChange={onModelChange} />
            <ParamControls
              params={params}
              onChange={onParamsChange}
              model={models.find((model) => model.id === selectedModel) ?? null}
            />
            <p className="inspector-note">
              Context window: {snapshot?.model.contextWindow?.toLocaleString() ?? "Unknown"} tokens
            </p>
          </section>
        ) : null}

        {section === "instructions" ? (
          <section>
            <SectionTitle
              title="Instructions"
              help="The selected agent persona is authoritative. Without an agent, this saved system prompt is used for typed chat and injected into the next Voice Live connection."
            />
            {snapshot?.instructions.editable === false ? (
              <div className="inspector-empty">
                <strong>{snapshot.agent.displayName}</strong>
                <p>The agent persona owns instructions. Edit the agent in Agents & workflows.</p>
              </div>
            ) : (
              <>
                <label htmlFor="conversation-system-prompt">System prompt</label>
                <textarea
                  id="conversation-system-prompt"
                  rows={8}
                  value={promptDraft}
                  placeholder="Optional conversation instructions"
                  onChange={(event) => setPromptDraft(event.target.value)}
                />
                <div className="inspector-actions">
                  <button type="button" onClick={() => void onSystemPromptChange(promptDraft)}>
                    Save
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setPromptDraft("");
                      void onSystemPromptChange("");
                    }}
                  >
                    Reset
                  </button>
                </div>
              </>
            )}
          </section>
        ) : null}

        {section === "tools" ? (
          <section>
            <SectionTitle
              title="Agent & tools"
              help="The agent provides an inherited tool set. Conversation overrides can add only server-approved tools or remove inherited tools. Authorization, approval, scope, egress, and SSRF checks run again when a tool executes."
            />
            <label>
              Agent
              <select
                value={snapshot?.agent.name ?? ""}
                disabled={!sessionId}
                onChange={(event) => void patch({ agentName: event.target.value || null })}
              >
                <option value="">Generic assistant</option>
                {agents.filter((agent) => agent.enabled).map((agent) => (
                  <option key={agent.name} value={agent.name}>{agent.displayName}</option>
                ))}
              </select>
            </label>
            <div className="tool-list">
              {tools.filter((tool) => tool.selectable).map((tool) => {
                const inherited = snapshot?.tools.inherited.includes(tool.name) ?? false;
                const effective = snapshot?.tools.effective.includes(tool.name) ?? false;
                return (
                  <label key={tool.name} className="tool-row">
                    <input
                      type="checkbox"
                      checked={effective}
                      disabled={!sessionId || !tool.available}
                      onChange={(event) => {
                        const inheritedTools = snapshot?.tools.inherited ?? [];
                        const added = new Set(snapshot?.tools.added ?? []);
                        const removed = new Set(snapshot?.tools.removed ?? []);
                        if (event.target.checked) {
                          removed.delete(tool.name);
                          if (!inheritedTools.includes(tool.name)) added.add(tool.name);
                        } else if (inheritedTools.includes(tool.name)) {
                          removed.add(tool.name);
                        } else {
                          added.delete(tool.name);
                        }
                        void patch({
                          toolOverrides: { added: [...added], removed: [...removed] },
                        });
                      }}
                    />
                    <span>
                      <strong>{tool.label}</strong>
                      <small>
                        {inherited ? "Inherited · " : ""}
                        {tool.risk} · {tool.requiresApproval ? "approval required" : "no approval"}
                      </small>
                    </span>
                  </label>
                );
              })}
            </div>
          </section>
        ) : null}

        {section === "context" ? (
          <section>
            <SectionTitle
              title="Context & documents"
              help="Session attachments are injected as bounded untrusted context. Selected ready library documents scope retrieval and citations. Upload limits and processing state remain server-authoritative."
            />
            <p>{snapshot?.attachments.length ?? 0} session attachments</p>
            <ul className="inspector-list">
              {snapshot?.libraryDocuments.map((document) => (
                <li key={document.id}>
                  <span>{document.filename}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${document.filename} from context`}
                    onClick={() =>
                      void patch({
                        libraryDocumentIds: [...selectedIds].filter((id) => id !== document.id),
                      })
                    }
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <h3>Recent library</h3>
            <ul className="inspector-list">
              {library?.recent.map((document) => (
                <li key={document.id}>
                  <span>{document.filename}<small>{document.status}</small></span>
                  <button
                    type="button"
                    disabled={!sessionId || document.status !== "ready" || selectedIds.has(document.id)}
                    onClick={() =>
                      void patch({ libraryDocumentIds: [...selectedIds, document.id] })
                    }
                  >
                    {selectedIds.has(document.id) ? "Added" : "Add"}
                  </button>
                </li>
              ))}
            </ul>
            {onOpenLibrary ? <button type="button" onClick={onOpenLibrary}>Open library</button> : null}
          </section>
        ) : null}

        {section === "memory" ? (
          <section>
            <SectionTitle
              title="Memory"
              help="These are memories scoped to your authenticated identity. Deleting removes only the selected owned memory. Disabled or unsupported backends are shown explicitly."
            />
            {memory?.status !== "ok" ? (
              <div className="inspector-empty">{memory?.detail ?? "Memory is unavailable."}</div>
            ) : (
              <ul className="inspector-list memory-list">
                {memory.items.map((item) => (
                  <li key={item.id}>
                    <span>{item.text}<small>{item.source}{item.createdAt ? ` · ${new Date(item.createdAt).toLocaleDateString()}` : ""}</small></span>
                    {memory.supportsDelete ? (
                      <button
                        type="button"
                        onClick={async () => {
                          await deleteMemory(item.id);
                          setMemory(await listMemories());
                        }}
                      >
                        Delete
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}

        {section === "usage" ? (
          <section>
            <SectionTitle
              title="Usage"
              help="Turn and monthly totals come from the caller-owned usage ledger. Token or cost values are shown as unknown when the provider did not report usage or no price was available."
            />
            <dl className="usage-grid">
              <div><dt>Conversation tokens</dt><dd>{snapshot?.sessionUsage.totalTokens.toLocaleString() ?? "Unknown"}</dd></div>
              <div><dt>Conversation cost</dt><dd>{snapshot ? money(snapshot.sessionUsage.totalCostMicroUsd) : "Unknown"}</dd></div>
              <div><dt>Monthly tokens</dt><dd>{snapshot?.monthlyUsage.totalTokens.toLocaleString() ?? "Unknown"}</dd></div>
              <div><dt>Monthly known cost</dt><dd>{snapshot ? money(snapshot.monthlyUsage.totalCostMicroUsd) : "Unknown"}</dd></div>
            </dl>
            {(snapshot?.sessionUsage.unknownUsageRequests ?? 0) > 0 ||
            (snapshot?.sessionUsage.costUnknownRequests ?? 0) > 0 ? (
              <p className="inspector-note">Some usage or cost is unknown; totals include known values only.</p>
            ) : null}
          </section>
        ) : null}

        {section === "voice" ? (
          <section>
            <SectionTitle
              title="Voice"
              help="Provider, model, voice, locale, turn detection, noise, echo, and interruption settings apply to the next connection. Conversation instructions, agent, and tools are injected by the server."
            />
            {voiceSettings ? (
              <VoiceSettingsPanel {...voiceSettings} locked={voiceLocked} />
            ) : (
              <div className="inspector-empty">Voice Live is not enabled.</div>
            )}
            <p className="inspector-note">Changes apply to the next Voice Live connection.</p>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
