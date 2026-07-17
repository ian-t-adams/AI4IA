"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
type LoadPhase = "idle" | "loading" | "ready" | "error";
const SECTIONS: readonly [Section, string][] = [
  ["model", "Model"],
  ["instructions", "Instructions"],
  ["tools", "Agent & tools"],
  ["context", "Context"],
  ["memory", "Memory"],
  ["usage", "Usage"],
  ["voice", "Voice"],
];

function money(microUsd: number): string {
  return `$${(microUsd / 1_000_000).toFixed(4)}`;
}

function costLabel(
  knownMicroUsd: number,
  unknownRequests: number,
  totalRequests: number,
): string {
  if (totalRequests > 0 && unknownRequests >= totalRequests && knownMicroUsd === 0) {
    return "Unknown";
  }
  if (unknownRequests > 0) return `Known subtotal ${money(knownMicroUsd)}`;
  return money(knownMicroUsd);
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
  refreshKey,
  models,
  agents,
  selectedModel,
  onModelChange,
  params,
  onParamsChange,
  systemPrompt,
  onSessionUpdated,
  onOpenLibrary,
  voiceSettings,
  voiceLocked,
  collapsed,
  onToggle,
}: {
  sessionId: string | null;
  refreshKey: number;
  models: ModelEntry[];
  agents: AgentSummary[];
  selectedModel: string | null;
  onModelChange: (model: string) => void;
  params: ChatParams;
  onParamsChange: (params: ChatParams) => void;
  systemPrompt: string;
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
  const [phases, setPhases] = useState<Record<string, LoadPhase>>({
    snapshot: "idle",
    tools: "idle",
    memory: "idle",
    library: "idle",
  });
  const [sectionErrors, setSectionErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [memoryPending, setMemoryPending] = useState<string | null>(null);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [memoryRetryId, setMemoryRetryId] = useState<string | null>(null);
  const [memoryConfirmId, setMemoryConfirmId] = useState<string | null>(null);
  const [toolQuery, setToolQuery] = useState("");
  const [promptDraft, setPromptDraft] = useState(systemPrompt);
  const generationRef = useRef(0);
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const memoryConfirmRef = useRef<HTMLButtonElement>(null);
  const memoryTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  const previousMemoryConfirmRef = useRef<string | null>(null);
  const drawer = useMediaQuery("(max-width: 1050px)") && !collapsed;
  const drawerFocus = useModalFocus<HTMLElement>(onToggle, drawer);

  useEffect(() => setPromptDraft(systemPrompt), [systemPrompt]);
  useEffect(
    () => () => {
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
    },
    [],
  );
  useEffect(() => {
    const previousId = previousMemoryConfirmRef.current;
    if (memoryConfirmId) {
      memoryConfirmRef.current?.focus();
    } else if (previousId) {
      const trigger = memoryTriggerRefs.current.get(previousId);
      if (trigger && !trigger.disabled) {
        trigger.focus();
      } else {
        document.getElementById("inspector-tab-memory")?.focus();
      }
    }
    previousMemoryConfirmRef.current = memoryConfirmId;
  }, [memoryConfirmId]);
  const showSaved = useCallback((message: string) => {
    if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
    setSaved(message);
    savedTimerRef.current = setTimeout(() => setSaved(null), 2000);
  }, []);

  const load = useCallback(async () => {
    const generation = ++generationRef.current;
    setSnapshot(null);
    setTools([]);
    setMemory(null);
    setLibrary(null);
    setSectionErrors({});
    if (!sessionId) {
      setPhases({
        snapshot: "idle",
        tools: "idle",
        memory: "idle",
        library: "idle",
      });
      return;
    }
    setLoading(true);
    setError(null);
    setPhases({
      snapshot: "loading",
      tools: "loading",
      memory: "loading",
      library: "loading",
    });
    const results = await Promise.allSettled([
      getInspector(sessionId),
      api.listTools(),
      listMemories(),
      getLibrarySummary(),
    ]);
    if (generation !== generationRef.current) return;
    const fail = (key: string, reason: unknown) => {
      const message =
        reason instanceof Error ? reason.message : `${key} unavailable`;
      setSectionErrors((current) => ({ ...current, [key]: message }));
      setPhases((current) => ({ ...current, [key]: "error" }));
    };
    if (results[0].status === "fulfilled") {
      setSnapshot(results[0].value);
      if (results[0].value.instructions.editable) {
        setPromptDraft(results[0].value.instructions.value ?? "");
      }
      setPhases((current) => ({ ...current, snapshot: "ready" }));
    } else fail("snapshot", results[0].reason);
    if (results[1].status === "fulfilled") {
      setTools(results[1].value);
      setPhases((current) => ({ ...current, tools: "ready" }));
    } else fail("tools", results[1].reason);
    if (results[2].status === "fulfilled") {
      setMemory(results[2].value);
      setPhases((current) => ({ ...current, memory: "ready" }));
    } else fail("memory", results[2].reason);
    if (results[3].status === "fulfilled") {
      setLibrary(results[3].value);
      setPhases((current) => ({ ...current, library: "ready" }));
    } else fail("library", results[3].reason);
    setLoading(false);
  }, [refreshKey, sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = useCallback(
    async (value: Parameters<typeof api.updateSession>[1]) => {
      if (
        !sessionId ||
        snapshot?.sessionId !== sessionId ||
        phases.snapshot !== "ready" ||
        saving
      ) {
        setError("Wait for the current conversation settings to finish loading.");
        return;
      }
      setSaving(true);
      setSaved(null);
      try {
        const updated = await api.updateSession(sessionId, value);
        onSessionUpdated(updated);
        await load();
        showSaved("Saved");
      } catch (reason) {
        setError((reason as Error).message);
      } finally {
        setSaving(false);
      }
    },
    [
      load,
      onSessionUpdated,
      phases.snapshot,
      saving,
      sessionId,
      showSaved,
      snapshot?.sessionId,
    ],
  );
  const canMutate =
    Boolean(sessionId) &&
    snapshot?.sessionId === sessionId &&
    phases.snapshot === "ready" &&
    !saving;
  const deleteMemoryItem = useCallback(async (id: string) => {
    setMemoryPending(id);
    setMemoryConfirmId(null);
    setMemoryError(null);
    setMemoryRetryId(null);
    try {
      await deleteMemory(id);
      setMemory(await listMemories());
    } catch (reason) {
      setMemoryError((reason as Error).message);
      setMemoryRetryId(id);
    } finally {
      setMemoryPending(null);
    }
  }, []);

  const selectedIds = useMemo(
    () => new Set(snapshot?.libraryDocuments.map((document) => document.id) ?? []),
    [snapshot],
  );
  const toolEntries = useMemo(() => {
    const byName = new Map(tools.map((tool) => [tool.name, tool]));
    for (const name of [
      ...(snapshot?.tools.inherited ?? []),
      ...(snapshot?.tools.added ?? []),
      ...(snapshot?.tools.removed ?? []),
      ...(snapshot?.tools.effective ?? []),
    ]) {
      if (!byName.has(name)) {
        byName.set(name, {
          name,
          label: name.replace(/[_:/-]+/g, " "),
          description: "Inherited agent tool",
          source: "agent",
          risk: "external",
          requiresApproval: false,
          scopes: [],
          available: true,
          selectable: false,
          ownership: "agent",
          typed: true,
          voice: snapshot?.tools.voiceEffective.includes(name) ?? false,
        });
      }
    }
    const query = toolQuery.trim().toLowerCase();
    return [...byName.values()]
      .filter(
        (tool) =>
          !query ||
          tool.label.toLowerCase().includes(query) ||
          tool.name.toLowerCase().includes(query) ||
          tool.description.toLowerCase().includes(query),
      )
      .sort((left, right) => left.label.localeCompare(right.label));
  }, [snapshot, toolQuery, tools]);

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
      role={drawer ? "dialog" : "complementary"}
      aria-modal={drawer ? true : undefined}
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
        {SECTIONS.map(([id, label], index) => (
          <button
            key={id}
            type="button"
            id={`inspector-tab-${id}`}
            role="tab"
            aria-selected={section === id}
            tabIndex={section === id ? 0 : -1}
            aria-controls={`inspector-panel-${id}`}
            onClick={() => setSection(id)}
            onKeyDown={(event) => {
              let next = index;
              if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                next = (index + 1) % SECTIONS.length;
              } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                next = (index - 1 + SECTIONS.length) % SECTIONS.length;
              } else if (event.key === "Home") {
                next = 0;
              } else if (event.key === "End") {
                next = SECTIONS.length - 1;
              } else {
                return;
              }
              event.preventDefault();
              const [nextId] = SECTIONS[next];
              setSection(nextId);
              document.getElementById(`inspector-tab-${nextId}`)?.focus();
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {error ? <div className="inspector-error" role="alert">{error}</div> : null}
      <div
        className="inspector-save-state"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {saving ? "Saving…" : saved ?? ""}
      </div>

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
            {sessionId && phases.snapshot === "loading" ? (
              <div className="inspector-empty">Loading model settings…</div>
            ) : (
              <>
                <ModelPicker
                  models={models}
                  value={selectedModel}
                  onChange={onModelChange}
                />
                <ParamControls
                  params={params}
                  onChange={onParamsChange}
                  model={models.find((model) => model.id === selectedModel) ?? null}
                />
              </>
            )}
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
            {phases.snapshot === "loading" ? (
              <div className="inspector-empty">Loading effective instructions…</div>
            ) : snapshot?.instructions.editable === false ? (
              <div className="inspector-empty">
                <strong>{snapshot.agent.displayName}</strong>
                <p>The agent persona owns instructions. Edit the agent in Agents & workflows.</p>
              </div>
            ) : snapshot ? (
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
                  <button
                    type="button"
                    disabled={!canMutate}
                    onClick={() => void patch({ systemPrompt: promptDraft })}
                  >
                    {saving ? "Saving…" : "Save"}
                  </button>
                  <button
                    type="button"
                    disabled={!canMutate}
                    onClick={() => {
                      setPromptDraft("");
                      void patch({ systemPrompt: "" });
                    }}
                  >
                    Reset
                  </button>
                </div>
              </>
            ) : (
              <div className="inspector-empty">
                Start or select a conversation before editing instructions.
              </div>
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
                disabled={!canMutate}
                onChange={(event) => void patch({ agentName: event.target.value || null })}
              >
                <option value="">Generic assistant</option>
                {agents.filter((agent) => agent.enabled).map((agent) => (
                  <option key={agent.name} value={agent.name}>{agent.displayName}</option>
                ))}
              </select>
            </label>
            <label>
              Search tools
              <input
                type="search"
                value={toolQuery}
                onChange={(event) => setToolQuery(event.target.value)}
                placeholder="Name or capability"
              />
            </label>
            {phases.tools === "loading" ? (
              <div className="inspector-empty">Loading tools…</div>
            ) : null}
            {phases.tools === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.tools}
              </div>
            ) : null}
            <div className="tool-list">
              {toolEntries.map((tool) => {
                const inherited = snapshot?.tools.inherited.includes(tool.name) ?? false;
                const effective = snapshot?.tools.effective.includes(tool.name) ?? false;
                const added = snapshot?.tools.added.includes(tool.name) ?? false;
                const removed = snapshot?.tools.removed.includes(tool.name) ?? false;
                return (
                  <label key={tool.name} className="tool-row">
                    <input
                      type="checkbox"
                      checked={effective}
                      disabled={
                        !canMutate ||
                        !tool.available ||
                        (!tool.selectable && !inherited)
                      }
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
                        {inherited ? "inherited · " : ""}
                        {added ? "added · " : ""}
                        {removed ? "removed · " : ""}
                        {effective ? "effective · " : "inactive · "}
                        {tool.source} · {tool.ownership} · {tool.risk}
                        {tool.requiresApproval ? " · approval required" : ""}
                        {tool.scopes.length ? ` · scopes: ${tool.scopes.join(", ")}` : ""}
                        {` · typed ${tool.typed ? "yes" : "no"} · voice ${tool.voice ? "yes" : "no"}`}
                        {!tool.available && tool.detail ? ` · ${tool.detail}` : ""}
                      </small>
                    </span>
                  </label>
                );
              })}
              {phases.tools === "ready" && toolEntries.length === 0 ? (
                <div className="inspector-empty">No tools match this search.</div>
              ) : null}
            </div>
          </section>
        ) : null}

        {section === "context" ? (
          <section>
            <SectionTitle
              title="Context & documents"
              help="Session attachments are injected as bounded untrusted context. Selected ready library documents scope retrieval and citations. Upload limits and processing state remain server-authoritative."
            />
            {phases.snapshot === "loading" || phases.library === "loading" ? (
              <div className="inspector-empty">Loading document context…</div>
            ) : null}
            {phases.snapshot === "error" || phases.library === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.snapshot ?? sectionErrors.library}
              </div>
            ) : null}
            {snapshot ? (
              <p className="inspector-note">
                Selection: {snapshot.librarySelectionMode === "legacy_all"
                  ? "All accessible documents (default)"
                  : `${snapshot.libraryDocuments.length} explicitly selected`}
                {" · "}
                {snapshot.attachments.length} session attachments
              </p>
            ) : null}
            <ul className="inspector-list">
              {snapshot?.attachments.map((document) => (
                <li key={document.id}>
                  <span>
                    {document.filename}
                    <small>
                      {document.contentType || "document"} · {document.size.toLocaleString()} bytes
                      {document.truncated ? " · truncated" : ""} · session context · citation metadata unavailable
                    </small>
                  </span>
                </li>
              ))}
            </ul>
            <ul className="inspector-list">
              {snapshot?.libraryDocuments.map((document) => (
                <li key={document.id}>
                  <span>
                    {document.filename}
                    <small>
                      {document.modality} · {document.size.toLocaleString()} bytes · {document.status}
                      {document.status === "ready"
                        ? ` · indexed ${document.chunkCount} chunks · citations ${document.citationReady ? "ready" : "partial"}`
                        : " · context pending"}
                      {document.error ? ` · ${document.error}` : ""}
                    </small>
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${document.filename} from context`}
                    disabled={!canMutate}
                    onClick={async () => {
                      if (!sessionId || !canMutate) return;
                      setSaving(true);
                      try {
                        const updated = await api.disassociateLibraryDocument(
                          sessionId,
                          document.id,
                        );
                        onSessionUpdated(updated);
                        await load();
                        showSaved("Document removed");
                      } catch (reason) {
                        setError((reason as Error).message);
                      } finally {
                        setSaving(false);
                      }
                    }}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <h3>Recent library</h3>
            {library ? (
              <p className="inspector-note">
                {library.total} total ·{" "}
                {Object.entries(library.byStatus)
                  .map(([name, count]) => `${count} ${name}`)
                  .join(", ") || "no status data"}
                {" · "}
                {Object.entries(library.byModality)
                  .map(([name, count]) => `${count} ${name}`)
                  .join(", ") || "no modality data"}
              </p>
            ) : null}
            <ul className="inspector-list">
              {library?.recent.map((document) => (
                <li key={document.id}>
                  <span>
                    {document.filename}
                    <small>
                      {document.modality} · {document.size.toLocaleString()} bytes · {document.status}
                      {document.error ? ` · ${document.error}` : ""}
                    </small>
                  </span>
                  <button
                    type="button"
                    aria-label={`${selectedIds.has(document.id) ? "Added" : "Add"} ${document.filename}`}
                    disabled={!canMutate || selectedIds.has(document.id)}
                    onClick={async () => {
                      if (!sessionId || !canMutate) return;
                      setSaving(true);
                      try {
                        const updated = await api.associateLibraryDocument(
                          sessionId,
                          document.id,
                        );
                        onSessionUpdated(updated);
                        await load();
                        showSaved(
                          document.status === "ready"
                            ? "Document added"
                            : "Document selected; context will activate when ready",
                        );
                      } catch (reason) {
                        setError((reason as Error).message);
                      } finally {
                        setSaving(false);
                      }
                    }}
                  >
                    {selectedIds.has(document.id) ? "Added" : "Add"}
                  </button>
                </li>
              ))}
              {phases.library === "ready" && library?.recent.length === 0 ? (
                <li className="inspector-empty">No library documents yet.</li>
              ) : null}
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
            {phases.memory === "loading" ? (
              <div className="inspector-empty">Loading memories…</div>
            ) : null}
            {phases.memory === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.memory}
              </div>
            ) : null}
            {memoryError ? (
              <div className="inspector-error" role="alert">
                {memoryError}
                {memoryRetryId ? (
                  <button
                    type="button"
                    onClick={() => {
                      const item = memory?.items.find(
                        (memoryItem) => memoryItem.id === memoryRetryId,
                      );
                      if (item) void deleteMemoryItem(item.id);
                    }}
                  >
                    Retry
                  </button>
                ) : null}
              </div>
            ) : null}
            {phases.memory === "ready" && memory?.status !== "ok" ? (
              <div className="inspector-empty">{memory?.detail ?? "Memory is unavailable."}</div>
            ) : null}
            {memory?.status === "ok" ? (
              <ul className="inspector-list memory-list">
                {memory.items.map((item) => (
                  <li key={item.id}>
                    <span>{item.text}<small>{item.source}{item.createdAt ? ` · ${new Date(item.createdAt).toLocaleDateString()}` : ""}</small></span>
                    {memory.supportsDelete ? (
                      memoryConfirmId === item.id ? (
                        <span className="memory-confirmation">
                          <span>Delete this memory?</span>
                          <button
                            ref={memoryConfirmRef}
                            type="button"
                            aria-label={`Confirm deletion of memory: ${item.text.slice(0, 80)}`}
                            disabled={memoryPending === item.id}
                            onClick={() => void deleteMemoryItem(item.id)}
                          >
                            {memoryPending === item.id ? "Deleting…" : "Confirm"}
                          </button>
                          <button
                            type="button"
                            aria-label={`Cancel deletion of memory: ${item.text.slice(0, 80)}`}
                            onClick={() => setMemoryConfirmId(null)}
                          >
                            Cancel
                          </button>
                        </span>
                      ) : (
                        <button
                          ref={(element) => {
                            if (element) {
                              memoryTriggerRefs.current.set(item.id, element);
                            } else {
                              memoryTriggerRefs.current.delete(item.id);
                            }
                          }}
                          type="button"
                          aria-label={`Delete memory: ${item.text.slice(0, 80)}`}
                          disabled={memoryPending === item.id}
                          onClick={() => setMemoryConfirmId(item.id)}
                        >
                          Delete
                        </button>
                      )
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
            {memory?.status === "ok" && memory.items.length === 0 ? (
              <div className="inspector-empty">No saved memories.</div>
            ) : null}
          </section>
        ) : null}

        {section === "usage" ? (
          <section>
            <SectionTitle
              title="Usage"
              help="Turn and rolling 30-day totals come from the caller-owned usage ledger. Token or cost values are shown as unknown when the provider did not report usage or no price was available."
            />
            {phases.snapshot === "loading" ? (
              <div className="inspector-empty">Loading usage…</div>
            ) : null}
            {phases.snapshot === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.snapshot}
              </div>
            ) : null}
            <dl className="usage-grid">
              <div><dt>Conversation requests</dt><dd>{snapshot?.sessionUsage.totalRequests.toLocaleString() ?? "Unavailable"}</dd></div>
              <div><dt>Conversation tokens</dt><dd>{snapshot?.sessionUsage.totalTokens.toLocaleString() ?? "Unavailable"}</dd></div>
              <div>
                <dt>Conversation cost</dt>
                <dd>
                  {snapshot
                    ? costLabel(
                        snapshot.sessionUsage.totalCostMicroUsd,
                        snapshot.sessionUsage.costUnknownRequests,
                        snapshot.sessionUsage.totalRequests,
                      )
                    : "Unavailable"}
                </dd>
              </div>
              <div><dt>Last 30 days tokens</dt><dd>{snapshot?.monthlyUsage.totalTokens.toLocaleString() ?? "Unavailable"}</dd></div>
              <div>
                <dt>Last 30 days cost</dt>
                <dd>
                  {snapshot
                    ? costLabel(
                        snapshot.monthlyUsage.totalCostMicroUsd,
                        snapshot.monthlyUsage.costUnknownRequests,
                        snapshot.monthlyUsage.totalRequests,
                      )
                    : "Unavailable"}
                </dd>
              </div>
              <div>
                <dt>Context pressure</dt>
                <dd>
                  {snapshot?.sessionUsage.latest?.usageKnown &&
                  snapshot.sessionUsage.latest.promptTokens != null &&
                  snapshot.model.contextWindow
                    ? `${Math.min(
                        100,
                        (snapshot.sessionUsage.latest.promptTokens /
                          snapshot.model.contextWindow) *
                          100,
                      ).toFixed(1)}%`
                    : "Unavailable"}
                </dd>
              </div>
            </dl>
            {(snapshot?.sessionUsage.unknownUsageRequests ?? 0) > 0 ||
            (snapshot?.sessionUsage.costUnknownRequests ?? 0) > 0 ? (
              <p className="inspector-note">Some usage or cost is unknown; totals include known values only.</p>
            ) : null}
            {snapshot?.sessionUsage.truncated ? (
              <p className="inspector-note">
                Partial coverage: newest {snapshot.sessionUsage.coveredRequests} requests,
                {snapshot.sessionUsage.coverageStart
                  ? ` from ${new Date(snapshot.sessionUsage.coverageStart).toLocaleString()}`
                  : ""}.
              </p>
            ) : null}
            {snapshot?.sessionUsage.latest ? (
              <p className="inspector-note">
                Latest turn: {snapshot.sessionUsage.latest.model} ·{" "}
                {new Date(snapshot.sessionUsage.latest.createdAt).toLocaleString()} ·{" "}
                {snapshot.sessionUsage.latest.usageComplete ? "complete usage" : "partial usage"}
              </p>
            ) : phases.snapshot === "ready" ? (
              <div className="inspector-empty">No metered turns in this conversation.</div>
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
