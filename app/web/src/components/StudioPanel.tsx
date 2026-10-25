"use client";

import { useState, type KeyboardEvent } from "react";
import type { AgentSummary, ModelEntry } from "@/lib/types";
import { AgentBuilder } from "./AgentBuilder";
import { DialogFrame } from "./DialogFrame";
import type { SurfaceVariant } from "./ModalShell";
import { WorkflowBuilder } from "./WorkflowBuilder";
import { McpServerBuilder } from "./McpServerBuilder";
import { DecisionPlayground } from "./DecisionPlayground";
import { allowWorkspaceNavigation } from "./useStudioDraft";

type Tab = "agents" | "workflows" | "tools" | "decisions";

const TAB_LABELS: Record<Tab, string> = {
  agents: "Agents",
  workflows: "Workflows",
  tools: "Custom tools",
  decisions: "Decisions",
};

export function StudioPanel({
  models,
  agents,
  runModel,
  customToolsEnabled = false,
  onAgentsChanged,
  onRun,
  onClose,
  variant = "dialog",
}: {
  models: ModelEntry[];
  agents: AgentSummary[];
  runModel: string | null;
  customToolsEnabled?: boolean;
  onAgentsChanged: () => Promise<void>;
  onRun: (sessionId: string) => void;
  onClose: () => void;
  /** "page" renders the studio as a destination page inside the shell. */
  variant?: SurfaceVariant;
}) {
  const [tab, setTab] = useState<Tab>("agents");
  const tabs: Tab[] = customToolsEnabled
    ? ["agents", "workflows", "tools", "decisions"]
    : ["agents", "workflows", "decisions"];
  const page = variant === "page";

  const selectTab = (nextTab: Tab, focus = false) => {
    if (nextTab !== tab && !allowWorkspaceNavigation()) return;
    setTab(nextTab);
    if (focus) document.getElementById(`studio-tab-${nextTab}`)?.focus();
  };
  const close = () => {
    if (allowWorkspaceNavigation()) onClose();
  };

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const index = tabs.indexOf(tab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    selectTab(tabs[nextIndex], true);
  };

  const surface = (
    <div
      data-testid="studio-surface"
      className="studio-surface"
      onClick={page ? undefined : (event) => event.stopPropagation()}
      style={{
        background: page ? undefined : "var(--bg-elevated)",
        color: "var(--fg)",
        width: page ? "100%" : "min(880px, 100%)",
        maxWidth: "100%",
        height: page ? undefined : "min(680px, 100%)",
        maxHeight: page ? undefined : "100%",
        minWidth: 0,
        borderRadius: "var(--radius)",
        border: page ? "none" : "1px solid var(--border)",
        padding: page ? 0 : "clamp(12px, 3vw, 24px)",
        display: "flex",
        flexDirection: "column",
        gap: 16,
        overflow: page ? undefined : "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 8,
          minWidth: 0,
        }}
      >
        <div
          role="tablist"
          aria-label="Studio sections"
          className="studio-tabs"
          style={{
            gridTemplateColumns: `repeat(var(--studio-tab-columns, ${tabs.length}), minmax(0, 1fr))`,
            flex: "1 1 auto",
          }}
        >
          {tabs.map((id) => (
            <button
              key={id}
              id={`studio-tab-${id}`}
              type="button"
              role="tab"
              aria-selected={tab === id}
              aria-controls="studio-tabpanel"
              tabIndex={tab === id ? 0 : -1}
              onClick={() => selectTab(id)}
              onKeyDown={onTabKeyDown}
              className="workspace-tab"
            >
              {TAB_LABELS[id]}
            </button>
          ))}
        </div>
        {page ? null : (
          <button
            type="button"
            onClick={close}
            aria-label="Close builder"
            style={{
              minWidth: 44,
              minHeight: 44,
              border: "none",
              borderRadius: 8,
              background: "transparent",
              color: "var(--fg)",
              fontSize: "1.2em",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        )}
      </div>

      <div
        id="studio-tabpanel"
        className="studio-tabpanel"
        role="tabpanel"
        aria-labelledby={`studio-tab-${tab}`}
        tabIndex={0}
        style={{
          flex: 1,
          minWidth: 0,
          minHeight: 0,
          display: "flex",
        }}
      >
        {tab === "agents" ? (
          <AgentBuilder
            agents={agents}
            models={models}
            customToolsEnabled={customToolsEnabled}
            onChanged={onAgentsChanged}
          />
        ) : tab === "tools" ? (
          <McpServerBuilder />
        ) : tab === "decisions" ? (
          <DecisionPlayground />
        ) : (
          <WorkflowBuilder agents={agents} runModel={runModel} onRun={onRun} />
        )}
      </div>
    </div>
  );

  if (page) return surface;

  return (
    <DialogFrame
      ariaLabel="Agents and workflows builder"
      onClose={close}
      zIndex={50}
      overlayPadding={8}
    >
      {surface}
    </DialogFrame>
  );
}
