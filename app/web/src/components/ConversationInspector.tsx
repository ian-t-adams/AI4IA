"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as api from "@/lib/api";
import {
  createMemory,
  deleteMemory,
  getInspector,
  getLibrarySummary,
  listMemories,
  updateMemory,
  type InspectorSnapshot,
  type LibrarySummary,
  type MemoryItem,
  type MemoryList,
} from "@/lib/inspector";
import type {
  AgentSummary,
  AttachmentCapabilities,
  ChatParams,
  ConversationDraftDefaults,
  ModelEntry,
  Session,
  ToolCatalogItem,
  ToolOverrides,
} from "@/lib/types";
import { HelpTooltip } from "./HelpTooltip";
import { ModelPicker } from "./ModelPicker";
import { ParamControls } from "./ParamControls";
import { VoiceSettingsPanel, type VoiceSettingsPanelProps } from "./VoiceSettingsPanel";
import { useMediaQuery } from "./useMediaQuery";
import { useModalFocus, useModalKeyDown } from "./useModalFocus";

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

function tokenLabel(
  knownTokens: number,
  unknownRequests: number,
  totalRequests: number,
): string {
  if (totalRequests > 0 && unknownRequests >= totalRequests && knownTokens === 0) {
    return "Unknown";
  }
  if (unknownRequests > 0) {
    const knownRequests = Math.max(0, totalRequests - unknownRequests);
    return `Known subtotal ${knownTokens.toLocaleString()} (${knownRequests}/${totalRequests} requests reported)`;
  }
  return knownTokens.toLocaleString();
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
  onSystemPromptChange,
  onSystemPromptDraftChange,
  draftDefaults,
  onDraftDefaultsChange,
  onSessionUpdated,
  onOpenLibrary,
  attachmentCapabilities,
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
  onSystemPromptChange: (value: string) => void;
  onSystemPromptDraftChange: () => void;
  draftDefaults: ConversationDraftDefaults;
  onDraftDefaultsChange: (value: ConversationDraftDefaults) => void;
  onSessionUpdated: (session: Session) => void;
  onOpenLibrary?: () => void;
  attachmentCapabilities: AttachmentCapabilities | null;
  voiceSettings?: Omit<VoiceSettingsPanelProps, "locked">;
  voiceLocked: boolean;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const [section, setSection] = useState<Section>("model");
  const [snapshot, setSnapshot] = useState<InspectorSnapshot | null>(null);
  const [tools, setTools] = useState<ToolCatalogItem[]>([]);
  const [draftInheritedTools, setDraftInheritedTools] = useState<string[]>([]);
  const [memory, setMemory] = useState<MemoryList | null>(null);
  const [library, setLibrary] = useState<LibrarySummary | null>(null);
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
  const [memoryCreateText, setMemoryCreateText] = useState("");
  const [memoryEditId, setMemoryEditId] = useState<string | null>(null);
  const [memoryEditText, setMemoryEditText] = useState("");
  const [toolQuery, setToolQuery] = useState("");
  const [promptDraft, setPromptDraft] = useState(systemPrompt);
  const sectionGenerationRef = useRef({
    snapshot: 0,
    tools: 0,
    memory: 0,
    library: 0,
  });
  const mutationGenerationRef = useRef(0);
  const mountedRef = useRef(true);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const memoryConfirmRef = useRef<HTMLButtonElement>(null);
  const memoryTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  const previousMemoryConfirmRef = useRef<string | null>(null);
  const drawerOpenerRef = useRef<HTMLButtonElement>(null);
  const drawerReturnFocusRef = useRef<HTMLElement | null>(null);
  const drawer = useMediaQuery("(max-width: 1050px)") && !collapsed;
  const drawerFocusRef = useModalFocus<HTMLElement>(drawer, drawerReturnFocusRef);
  const onDrawerKeyDown = useModalKeyDown<HTMLElement>(onToggle, drawer);

  useEffect(() => setPromptDraft(systemPrompt), [systemPrompt]);
  useEffect(
    () => () => {
      mountedRef.current = false;
      mutationGenerationRef.current += 1;
    },
    [],
  );
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

  const loadSnapshot = useCallback(async () => {
    const capturedSession = sessionId;
    const generation = ++sectionGenerationRef.current.snapshot;
    setSnapshot(null);
    setSectionErrors((current) => {
      const next = { ...current };
      delete next.snapshot;
      return next;
    });
    if (!capturedSession) {
      setPhases((current) => ({ ...current, snapshot: "idle" }));
      return;
    }
    setPhases((current) => ({ ...current, snapshot: "loading" }));
    try {
      const value = await getInspector(capturedSession);
      if (
        generation !== sectionGenerationRef.current.snapshot ||
        activeSessionRef.current !== capturedSession
      ) return;
      setSnapshot(value);
      if (value.instructions.editable) setPromptDraft(value.instructions.value ?? "");
      setPhases((current) => ({ ...current, snapshot: "ready" }));
    } catch (reason) {
      if (
        generation !== sectionGenerationRef.current.snapshot ||
        activeSessionRef.current !== capturedSession
      ) return;
      setSectionErrors((current) => ({
        ...current,
        snapshot: (reason as Error).message || "Conversation settings unavailable",
      }));
      setPhases((current) => ({ ...current, snapshot: "error" }));
    }
  }, [sessionId]);

  const loadTools = useCallback(async () => {
    const capturedSession = sessionId;
    const generation = ++sectionGenerationRef.current.tools;
    setTools([]);
    setPhases((current) => ({ ...current, tools: "loading" }));
    try {
      const value = await api.getToolCatalog(
        capturedSession,
        capturedSession ? null : draftDefaults.agentName,
      );
      if (
        generation !== sectionGenerationRef.current.tools ||
        activeSessionRef.current !== capturedSession
      ) return;
      setTools(value.tools);
      setDraftInheritedTools(value.inheritedTools);
      setPhases((current) => ({ ...current, tools: "ready" }));
    } catch (reason) {
      if (
        generation !== sectionGenerationRef.current.tools ||
        activeSessionRef.current !== capturedSession
      ) return;
      setSectionErrors((current) => ({ ...current, tools: (reason as Error).message }));
      setPhases((current) => ({ ...current, tools: "error" }));
    }
  }, [draftDefaults.agentName, sessionId]);

  const loadMemory = useCallback(async () => {
    const generation = ++sectionGenerationRef.current.memory;
    setMemory(null);
    setPhases((current) => ({ ...current, memory: "loading" }));
    try {
      const value = await listMemories();
      if (generation !== sectionGenerationRef.current.memory) return;
      setMemory(value);
      setPhases((current) => ({ ...current, memory: "ready" }));
    } catch (reason) {
      if (generation !== sectionGenerationRef.current.memory) return;
      setSectionErrors((current) => ({ ...current, memory: (reason as Error).message }));
      setPhases((current) => ({ ...current, memory: "error" }));
    }
  }, []);

  const loadLibrary = useCallback(async () => {
    const generation = ++sectionGenerationRef.current.library;
    setLibrary(null);
    setPhases((current) => ({ ...current, library: "loading" }));
    try {
      const value = await getLibrarySummary();
      if (generation !== sectionGenerationRef.current.library) return;
      setLibrary(value);
      setPhases((current) => ({ ...current, library: "ready" }));
    } catch (reason) {
      if (generation !== sectionGenerationRef.current.library) return;
      setSectionErrors((current) => ({ ...current, library: (reason as Error).message }));
      setPhases((current) => ({ ...current, library: "error" }));
    }
  }, []);

  const load = useCallback(async () => {
    await Promise.allSettled([
      loadSnapshot(),
      loadTools(),
      loadMemory(),
      loadLibrary(),
    ]);
  }, [loadLibrary, loadMemory, loadSnapshot, loadTools]);

  useEffect(() => {
    void refreshKey;
    setError(null);
    void load();
  }, [load, refreshKey]);

  useEffect(() => {
    mutationGenerationRef.current += 1;
    setSaving(false);
  }, [sessionId]);

  const runSessionMutation = useCallback(
    async (operation: () => Promise<Session>, successMessage: string) => {
      if (
        !sessionId ||
        snapshot?.sessionId !== sessionId ||
        phases.snapshot !== "ready" ||
        saving
      ) {
        setError("Wait for the current conversation settings to finish loading.");
        return;
      }
      const capturedSession = sessionId;
      const generation = ++mutationGenerationRef.current;
      setSaving(true);
      setSaved(null);
      try {
        const updated = await operation();
        if (
          generation !== mutationGenerationRef.current ||
          activeSessionRef.current !== capturedSession
        ) return;
        onSessionUpdated(updated);
        await loadSnapshot();
        if (
          generation !== mutationGenerationRef.current ||
          activeSessionRef.current !== capturedSession
        ) return;
        showSaved(successMessage);
      } catch (reason) {
        if (
          generation === mutationGenerationRef.current &&
          activeSessionRef.current === capturedSession
        ) setError(api.apiErrorDetail(reason));
      } finally {
        if (mountedRef.current && activeSessionRef.current === capturedSession) {
          setSaving(false);
        }
      }
    },
    [
      loadSnapshot,
      onSessionUpdated,
      phases.snapshot,
      saving,
      sessionId,
      showSaved,
      snapshot?.sessionId,
    ],
  );

  const patch = useCallback(
    async (value: Parameters<typeof api.updateSession>[1]) => {
      if (!sessionId) {
        setError("This conversation has not been created yet.");
        return;
      }
      await runSessionMutation(
        () => api.updateSession(sessionId, value),
        "Saved",
      );
    },
    [runSessionMutation, sessionId],
  );
  const canMutate =
    Boolean(sessionId) &&
    snapshot?.sessionId === sessionId &&
    phases.snapshot === "ready" &&
    !saving;
  const updateDraft = useCallback(
    (value: Partial<ConversationDraftDefaults>) => {
      onDraftDefaultsChange({ ...draftDefaults, ...value });
      setSaved(null);
    },
    [draftDefaults, onDraftDefaultsChange],
  );
  const loading = Object.values(phases).some((phase) => phase === "loading");
  const createMemoryItem = useCallback(async () => {
    const text = memoryCreateText.trim();
    if (!text) return;
    setMemoryPending("create");
    setMemoryError(null);
    setMemoryRetryId(null);
    try {
      await createMemory(text);
      setMemoryCreateText("");
      setMemory(await listMemories());
      showSaved("Memory created");
    } catch (reason) {
      setMemoryError((reason as Error).message);
    } finally {
      setMemoryPending(null);
    }
  }, [memoryCreateText, showSaved]);
  const updateMemoryItem = useCallback(async (item: MemoryItem) => {
    const text = memoryEditText.trim();
    if (!text || !item.etag) return;
    setMemoryPending(item.id);
    setMemoryError(null);
    setMemoryRetryId(null);
    try {
      await updateMemory(item.id, text, item.etag);
      setMemoryEditId(null);
      setMemoryEditText("");
      setMemory(await listMemories());
      showSaved("Memory updated");
    } catch (reason) {
      setMemoryError((reason as Error).message);
    } finally {
      setMemoryPending(null);
    }
  }, [memoryEditText, showSaved]);
  const deleteMemoryItem = useCallback(async (item: MemoryItem) => {
    setMemoryPending(item.id);
    setMemoryConfirmId(null);
    setMemoryError(null);
    setMemoryRetryId(null);
    try {
      await deleteMemory(item.id, item.etag);
      if (memoryEditId === item.id) {
        setMemoryEditId(null);
        setMemoryEditText("");
      }
      setMemory(await listMemories());
      showSaved("Memory deleted");
    } catch (reason) {
      setMemoryError((reason as Error).message);
      setMemoryRetryId(item.id);
    } finally {
      setMemoryPending(null);
    }
  }, [memoryEditId, showSaved]);

  const selectedIds = useMemo(
    () => new Set(snapshot?.libraryDocuments.map((document) => document.id) ?? []),
    [snapshot],
  );
  const toolEntries = useMemo(() => {
    const query = toolQuery.trim().toLowerCase();
    return tools
      .filter(
        (tool) =>
          !query ||
          tool.label.toLowerCase().includes(query) ||
          tool.name.toLowerCase().includes(query) ||
          tool.description.toLowerCase().includes(query),
      )
      .sort((left, right) => left.label.localeCompare(right.label));
  }, [toolQuery, tools]);
  // What the currently-selected agent does, regardless of whether it came from
  // a live session snapshot or a not-yet-started conversation's draft default.
  const agentDescription = useMemo(() => {
    if (sessionId) return snapshot?.agent.description || null;
    if (!draftDefaults.agentName) return null;
    return agents.find((agent) => agent.name === draftDefaults.agentName)?.description || null;
  }, [agents, draftDefaults.agentName, sessionId, snapshot]);

  if (collapsed) {
    return (
      <aside className="conversation-inspector collapsed" aria-label="Conversation inspector">
        <button
          ref={(element) => {
            drawerOpenerRef.current = element;
            if (element) drawerReturnFocusRef.current = element;
          }}
          type="button"
          onClick={() => {
            drawerReturnFocusRef.current = drawerOpenerRef.current;
            onToggle();
          }}
          aria-label="Open conversation inspector"
        >
          ‹
        </button>
      </aside>
    );
  }

  return (
    <aside
      ref={drawerFocusRef}
      onKeyDown={onDrawerKeyDown}
      className="conversation-inspector"
      aria-label="Conversation inspector"
      aria-busy={loading}
      role={drawer ? "dialog" : "complementary"}
      aria-modal={drawer ? true : undefined}
    >
      <header className="inspector-header">
        <div>
          <strong>{sessionId ? "Conversation" : "New conversation defaults"}</strong>
          <span aria-live="polite">
            {loading ? "Updating…" : sessionId ? "Inspector" : "Draft"}
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
        {saving
          ? "Saving…"
          : sessionId
            ? saved ?? ""
            : "Draft — applied when the conversation starts"}
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
            ) : sessionId && phases.snapshot === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.snapshot}
                <button type="button" onClick={() => void loadSnapshot()}>Retry</button>
              </div>
            ) : (
              <>
                <ModelPicker
                  models={models}
                  value={selectedModel}
                  onChange={onModelChange}
                  disabled={Boolean(sessionId) && !canMutate}
                />
                <ParamControls
                  params={params}
                  onChange={onParamsChange}
                  model={models.find((model) => model.id === selectedModel) ?? null}
                  disabled={Boolean(sessionId) && !canMutate}
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
            {!sessionId ? (
              <>
                <label htmlFor="conversation-system-prompt">System prompt</label>
                <textarea
                  id="conversation-system-prompt"
                  rows={8}
                  value={promptDraft}
                  placeholder="Optional conversation instructions"
                  onChange={(event) => {
                    setPromptDraft(event.target.value);
                    onSystemPromptChange(event.target.value);
                  }}
                />
                <p className="inspector-note">
                  This draft is included when the first message, attachment, or Voice
                  Live connection creates the conversation.
                </p>
                <button
                  type="button"
                  onClick={() => {
                    setPromptDraft("");
                    onSystemPromptChange("");
                  }}
                >
                  Reset
                </button>
              </>
            ) : phases.snapshot === "loading" ? (
              <div className="inspector-empty">Loading effective instructions…</div>
            ) : phases.snapshot === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.snapshot}
                <button type="button" onClick={() => void loadSnapshot()}>Retry</button>
              </div>
            ) : snapshot?.instructions.editable === false ? (
              <>
                <p className="inspector-note">
                  <strong>{snapshot.agent.displayName}</strong>
                  {snapshot.agent.description ? ` — ${snapshot.agent.description}` : null}
                </p>
                <label htmlFor="conversation-agent-instructions">
                  Agent instructions (read-only)
                </label>
                <textarea
                  id="conversation-agent-instructions"
                  rows={8}
                  readOnly
                  value={snapshot.instructions.value ?? ""}
                />
                <p className="inspector-note">
                  {snapshot.instructions.agentSource === "curated"
                    ? "This is a built-in agent managed by your administrator, so its instructions can't be changed here. To customize it, create your own agent in Agents & workflows — you can paste these instructions in as a starting point."
                    : "You own this agent. Edit its instructions in Agents & workflows."}
                </p>
              </>
            ) : snapshot ? (
              <>
                <label htmlFor="conversation-system-prompt">System prompt</label>
                <textarea
                  id="conversation-system-prompt"
                  rows={8}
                  value={promptDraft}
                  placeholder="Optional conversation instructions"
                  onChange={(event) => {
                    setPromptDraft(event.target.value);
                    onSystemPromptDraftChange();
                  }}
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
            ) : null}
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
                value={sessionId ? snapshot?.agent.name ?? "" : draftDefaults.agentName ?? ""}
                disabled={sessionId ? !canMutate : false}
                onChange={(event) => {
                  const agentName = event.target.value || null;
                  if (sessionId) {
                    void patch({ agentName });
                  } else {
                    updateDraft({ agentName });
                  }
                }}
              >
                <option value="">Generic assistant</option>
                {agents.filter((agent) => agent.enabled).map((agent) => (
                  <option key={agent.name} value={agent.name} title={agent.description || undefined}>
                    {agent.displayName}
                  </option>
                ))}
              </select>
            </label>
            {agentDescription ? <p className="inspector-note">{agentDescription}</p> : null}
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
                <button type="button" onClick={() => void loadTools()}>Retry</button>
              </div>
            ) : null}
            {phases.tools === "ready" && toolEntries.length > 0 ? (
              <div className="tool-list-header">
                <span>Tools</span>
                <HelpTooltip label="How to read the tool list" size="sm">
                  Each row shows, in order: whether it&apos;s <strong>inherited</strong>{" "}
                  from the agent, <strong>added</strong>/<strong>removed</strong> by
                  this conversation&apos;s overrides, and whether it&apos;s{" "}
                  <strong>effective</strong> right now. Then its{" "}
                  <strong>source</strong> and <strong>ownership</strong>; its{" "}
                  <strong>risk</strong> tier (safe, external, or destructive); whether
                  it <strong>needs approval</strong> — chat has no live approval
                  prompt, so this means the tool is unavailable, not merely
                  slower; its <strong>scopes</strong> (specific permissions it can
                  exercise); and
                  whether it&apos;s <strong>typed</strong> (schema-validated inputs) or{" "}
                  <strong>voice</strong>-capable (works during a Voice Live call).
                </HelpTooltip>
              </div>
            ) : null}
            <div className="tool-list">
              {toolEntries.map((tool, index) => {
                const inherited = sessionId
                  ? snapshot?.tools.inherited.includes(tool.name) ?? false
                  : draftInheritedTools.includes(tool.name);
                const effective = sessionId
                  ? snapshot?.tools.effective.includes(tool.name) ?? false
                  : (
                      inherited ||
                      draftDefaults.toolOverrides.added.includes(tool.name)
                    ) &&
                    !draftDefaults.toolOverrides.removed.includes(tool.name);
                const added = sessionId
                  ? snapshot?.tools.added.includes(tool.name) ?? false
                  : draftDefaults.toolOverrides.added.includes(tool.name);
                const removed = sessionId
                  ? snapshot?.tools.removed.includes(tool.name) ?? false
                  : draftDefaults.toolOverrides.removed.includes(tool.name);
                const lockedOut = tool.available && !tool.selectable && !inherited;
                // Index-based, not name-based: aria-describedby (like all
                // IDREFS attributes) is a whitespace-separated token list, so
                // an id built from a raw tool name containing a space (e.g. a
                // discovered tool literally named "foo bar") would silently
                // split into two bogus tokens and break the association.
                // A per-render index is always whitespace-free and unique.
                const toolInputId = `tool-input-${index}`;
                const toolStatusId = `tool-status-${index}`;
                const toolLockedId = `tool-locked-${index}`;
                return (
                  // A <label> can only own one interactive control; nesting the
                  // HelpTooltip's own button inside it (as before) folded "Help:
                  // …" into the checkbox's computed name alongside the rest of
                  // the row's text. Use an explicit id/htmlFor pair scoped to
                  // just the visible label text, keep the tooltip as a sibling,
                  // and carry the rich status/lockout detail via
                  // aria-describedby instead of the accessible name.
                  <div key={tool.name} className="tool-row">
                    <input
                      type="checkbox"
                      id={toolInputId}
                      aria-describedby={lockedOut ? `${toolStatusId} ${toolLockedId}` : toolStatusId}
                      checked={effective}
                      disabled={
                        (sessionId ? !canMutate : false) ||
                        !tool.available ||
                        (!tool.selectable && !inherited)
                      }
                      onChange={(event) => {
                        const inheritedTools = sessionId
                          ? snapshot?.tools.inherited ?? []
                          : draftInheritedTools;
                        const currentOverrides: ToolOverrides = sessionId
                          ? {
                              added: snapshot?.tools.added ?? [],
                              removed: snapshot?.tools.removed ?? [],
                            }
                          : draftDefaults.toolOverrides;
                        const added = new Set(currentOverrides.added);
                        const removed = new Set(currentOverrides.removed);
                        if (event.target.checked) {
                          removed.delete(tool.name);
                          if (!inheritedTools.includes(tool.name)) added.add(tool.name);
                        } else if (inheritedTools.includes(tool.name)) {
                          added.delete(tool.name);
                          removed.add(tool.name);
                        } else {
                          added.delete(tool.name);
                        }
                        const toolOverrides = {
                          added: [...added],
                          removed: [...removed],
                        };
                        if (sessionId) {
                          void patch({ toolOverrides });
                        } else {
                          updateDraft({ toolOverrides });
                        }
                      }}
                    />
                    <span>
                      <label htmlFor={toolInputId}>
                        <strong>{tool.label}</strong>
                      </label>
                      {tool.description ? (
                        <HelpTooltip label={`${tool.label} description`} size="sm">
                          {tool.description}
                        </HelpTooltip>
                      ) : null}
                      <small id={toolStatusId}>
                        {inherited ? "inherited · " : ""}
                        {added ? "added · " : ""}
                        {removed ? "removed · " : ""}
                        {effective ? "effective · " : "inactive · "}
                        {tool.source || "source unknown"} · {tool.ownership || "ownership unknown"} ·{" "}
                        {tool.risk ?? "risk unknown"}
                        {tool.requiresApproval === true
                          ? " · approval required"
                          : tool.requiresApproval === false
                            ? " · approval not required"
                            : " · approval unknown"}
                        {tool.scopes === null
                          ? " · scopes unknown"
                          : tool.scopes.length
                            ? ` · scopes: ${tool.scopes.join(", ")}`
                            : " · scopes: none"}
                        {` · typed ${tool.typed === null ? "unknown" : tool.typed ? "yes" : "no"} · voice ${tool.voice === null ? "unknown" : tool.voice ? "yes" : "no"}`}
                        {!tool.available && tool.detail ? ` · ${tool.detail}` : ""}
                      </small>
                      {lockedOut ? (
                        <small id={toolLockedId}>
                          Can&apos;t enable from here — it needs approval, scopes, or
                          restricted access that only a pre-built agent can grant, not
                          an ad-hoc conversation toggle.
                        </small>
                      ) : null}
                    </span>
                  </div>
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
                {phases.snapshot === "error" ? (
                  <button type="button" onClick={() => void loadSnapshot()}>Retry conversation context</button>
                ) : null}
                {phases.library === "error" ? (
                  <button type="button" onClick={() => void loadLibrary()}>Retry library insight</button>
                ) : null}
              </div>
            ) : null}
            {snapshot ? (
              <p className="inspector-note">
                Selection: {snapshot.librarySelectionMode === "legacy_all"
                  ? "All accessible documents (default)"
                  : `${snapshot.libraryDocuments.length} explicitly selected`}
                {" · "}
                {snapshot.attachments.length} session attachments
                {" · "}snapshot generated {new Date(snapshot.generatedAt).toLocaleTimeString()}
              </p>
            ) : !sessionId ? (
              <p className="inspector-note">
                Selection: {draftDefaults.libraryDocumentIds.length} documents for the
                new conversation.
              </p>
            ) : null}
            <p className="inspector-note">
              {attachmentCapabilities
                ? `Attach via ${attachmentCapabilities.ingestPath}; ${attachmentCapabilities.modalities.join(", ")}; ${attachmentCapabilities.maxBytes.toLocaleString()} bytes each; ${attachmentCapabilities.maxPerSessionDocuments} per conversation${
                    attachmentCapabilities.maxPerUserDocuments
                      ? `; ${attachmentCapabilities.maxPerUserDocuments} per library`
                      : ""
                  }.`
                : "Attachment capabilities unavailable."}
              {library
                ? ` Library insight generated ${new Date(library.generatedAt).toLocaleTimeString()}.`
                : ""}
            </p>
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
                    onClick={() => {
                      if (!sessionId) return;
                      void runSessionMutation(
                        () => api.disassociateLibraryDocument(sessionId, document.id),
                        "Document removed",
                      );
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
              {library?.recent.map((document) => {
                const selected = sessionId
                  ? selectedIds.has(document.id)
                  : draftDefaults.libraryDocumentIds.includes(document.id);
                return (
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
                    aria-label={`${selected ? (sessionId ? "Added" : "Remove") : "Add"} ${document.filename}`}
                    disabled={sessionId ? !canMutate || selected : false}
                    onClick={() => {
                      if (sessionId) {
                        void runSessionMutation(
                          () => api.associateLibraryDocument(sessionId, document.id),
                          document.status === "ready"
                            ? "Document added"
                            : "Document selected; context will activate when ready",
                        );
                      } else {
                        updateDraft({
                          libraryDocumentIds: selected
                            ? draftDefaults.libraryDocumentIds.filter(
                                (id) => id !== document.id,
                              )
                            : [...draftDefaults.libraryDocumentIds, document.id],
                        });
                      }
                    }}
                  >
                    {selected ? (sessionId ? "Added" : "Remove") : "Add"}
                  </button>
                </li>
              )})}
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
              help="These durable memories are scoped to your authenticated identity. Create and edit make user-locked memories that automatic consolidation cannot change. Updates and deletes use version checks so a stale browser cannot overwrite newer data."
            />
            {phases.memory === "loading" ? (
              <div className="inspector-empty">Loading memories…</div>
            ) : null}
            {phases.memory === "error" ? (
              <div className="inspector-error" role="alert">
                {sectionErrors.memory}
                <button type="button" onClick={() => void loadMemory()}>Retry</button>
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
                      if (item) void deleteMemoryItem(item);
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
            {memory?.status === "ok" && memory.supportsCreate ? (
              <form
                className="memory-editor"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createMemoryItem();
                }}
              >
                <label htmlFor="new-memory">Add a memory</label>
                <textarea
                  id="new-memory"
                  rows={3}
                  maxLength={2000}
                  value={memoryCreateText}
                  placeholder="A durable preference or fact to remember"
                  disabled={memoryPending !== null}
                  onChange={(event) => setMemoryCreateText(event.target.value)}
                />
                <div className="inspector-actions">
                  <button
                    type="submit"
                    disabled={!memoryCreateText.trim() || memoryPending !== null}
                  >
                    {memoryPending === "create" ? "Saving…" : "Save memory"}
                  </button>
                </div>
              </form>
            ) : null}
            {memory?.status === "ok" ? (
              <ul className="inspector-list memory-list">
                {memory.items.map((item) => {
                  const editing = memoryEditId === item.id;
                  return (
                    <li key={item.id}>
                      {editing ? (
                        <form
                          className="memory-editor"
                          onSubmit={(event) => {
                            event.preventDefault();
                            void updateMemoryItem(item);
                          }}
                        >
                          <label htmlFor={`memory-edit-${item.id}`}>
                            Edit memory
                          </label>
                          <textarea
                            id={`memory-edit-${item.id}`}
                            rows={3}
                            maxLength={2000}
                            value={memoryEditText}
                            disabled={memoryPending !== null}
                            onChange={(event) => setMemoryEditText(event.target.value)}
                          />
                          <div className="inspector-actions">
                            <button
                              type="submit"
                              disabled={
                                !item.etag ||
                                !memoryEditText.trim() ||
                                memoryPending !== null
                              }
                            >
                              {memoryPending === item.id ? "Saving…" : "Save"}
                            </button>
                            <button
                              type="button"
                              disabled={memoryPending !== null}
                              onClick={() => {
                                setMemoryEditId(null);
                                setMemoryEditText("");
                              }}
                            >
                              Cancel
                            </button>
                          </div>
                        </form>
                      ) : (
                        <>
                          <span>
                            {item.text}
                            <small>
                              {item.source}
                              {item.createdAt
                                ? ` · ${new Date(item.createdAt).toLocaleDateString()}`
                                : ""}
                              {item.origin === "user" ? " · user managed" : ""}
                            </small>
                          </span>
                          <span className="inspector-actions">
                            {memory.supportsEdit && item.etag ? (
                              <button
                                type="button"
                                aria-label={`Edit memory: ${item.text.slice(0, 80)}`}
                                disabled={memoryPending !== null}
                                onClick={() => {
                                  setMemoryEditId(item.id);
                                  setMemoryEditText(item.text);
                                  setMemoryConfirmId(null);
                                }}
                              >
                                Edit
                              </button>
                            ) : null}
                            {memory.supportsDelete ? (
                              memoryConfirmId === item.id ? (
                                <span className="memory-confirmation">
                                  <span>Delete this memory?</span>
                                  <button
                                    ref={memoryConfirmRef}
                                    type="button"
                                    aria-label={`Confirm deletion of memory: ${item.text.slice(0, 80)}`}
                                    disabled={memoryPending !== null}
                                    onClick={() => void deleteMemoryItem(item)}
                                  >
                                    {memoryPending === item.id ? "Deleting…" : "Confirm"}
                                  </button>
                                  <button
                                    type="button"
                                    aria-label={`Cancel deletion of memory: ${item.text.slice(0, 80)}`}
                                    disabled={memoryPending !== null}
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
                                  disabled={memoryPending !== null}
                                  onClick={() => {
                                    setMemoryConfirmId(item.id);
                                    setMemoryEditId(null);
                                    setMemoryEditText("");
                                  }}
                                >
                                  Delete
                                </button>
                              )
                            ) : null}
                          </span>
                        </>
                      )}
                    </li>
                  );
                })}
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
                <button type="button" onClick={() => void loadSnapshot()}>Retry</button>
              </div>
            ) : null}
            <dl className="usage-grid">
              <div><dt>Conversation requests</dt><dd>{snapshot?.sessionUsage.totalRequests.toLocaleString() ?? "Unavailable"}</dd></div>
              <div>
                <dt>Conversation tokens</dt>
                <dd>
                  {snapshot
                    ? tokenLabel(
                        snapshot.sessionUsage.totalTokens,
                        snapshot.sessionUsage.unknownUsageRequests,
                        snapshot.sessionUsage.totalRequests,
                      )
                    : "Unavailable"}
                </dd>
              </div>
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
              <div>
                <dt>Last 30 days tokens</dt>
                <dd>
                  {snapshot
                    ? tokenLabel(
                        snapshot.monthlyUsage.totalTokens,
                        snapshot.monthlyUsage.unknownUsageRequests,
                        snapshot.monthlyUsage.totalRequests,
                      )
                    : "Unavailable"}
                </dd>
              </div>
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
                <dt>Latest-turn prompt pressure</dt>
                <dd>
                  {snapshot?.sessionUsage.latest?.usageKnown &&
                  snapshot.sessionUsage.latest.promptTokens != null &&
                  snapshot.model.contextWindow &&
                  snapshot.sessionUsage.latest.model === snapshot.model.id
                    ? `${Math.min(
                        100,
                        (snapshot.sessionUsage.latest.promptTokens /
                          snapshot.model.contextWindow) *
                          100,
                      ).toFixed(1)}%`
                    : snapshot?.sessionUsage.latest &&
                        snapshot.sessionUsage.latest.model !== snapshot.model.id
                      ? `Unavailable (latest turn used ${snapshot.sessionUsage.latest.model})`
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
                {!snapshot.sessionUsage.latest.usageKnown
                  ? "usage unknown"
                  : snapshot.sessionUsage.latest.usageComplete
                    ? "complete usage"
                    : "partial usage"}
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
