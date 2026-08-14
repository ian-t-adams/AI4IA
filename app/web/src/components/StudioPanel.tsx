"use client";

import { useState, type KeyboardEvent } from "react";
import type { AgentSummary, ModelEntry } from "@/lib/types";
import { AgentBuilder } from "./AgentBuilder";
import { DialogFrame } from "./DialogFrame";
import { WorkflowBuilder } from "./WorkflowBuilder";
import { McpServerBuilder } from "./McpServerBuilder";

type Tab = "agents" | "workflows" | "tools";

const TAB_LABELS: Record<Tab, string> = {
  agents: "Agents",
  workflows: "Workflows",
  tools: "Custom tools",
};

export function StudioPanel({
  models,
  agents,
  runModel,
  customToolsEnabled = false,
  onAgentsChanged,
  onRun,
  onClose,
}: {
  models: ModelEntry[];
  agents: AgentSummary[];
  runModel: string | null;
  customToolsEnabled?: boolean;
  onAgentsChanged: () => Promise<void>;
  onRun: (sessionId: string) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("agents");
  const tabs: Tab[] = customToolsEnabled
    ? ["agents", "workflows", "tools"]
    : ["agents", "workflows"];

  const selectTab = (nextTab: Tab, focus = false) => {
    setTab(nextTab);
    if (focus) document.getElementById(`studio-tab-${nextTab}`)?.focus();
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

  return (
    <DialogFrame
      ariaLabel="Agents and workflows builder"
      onClose={onClose}
      zIndex={50}
      overlayPadding={8}
    >
      <div
        data-testid="studio-surface"
        onClick={(event) => event.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(880px, 100%)",
          maxWidth: "100%",
          height: "min(680px, 100%)",
          maxHeight: "100%",
          minWidth: 0,
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: "clamp(12px, 3vw, 24px)",
          display: "flex",
          flexDirection: "column",
          gap: 16,
          overflow: "hidden",
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
            style={{
              display: "flex",
              gap: 8,
              flex: "1 1 auto",
              minWidth: 0,
              maxWidth: "100%",
              overflowX: "auto",
              paddingBottom: 2,
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
                style={{
                  minHeight: 44,
                  flex: "0 0 auto",
                  padding: "8px 16px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: tab === id ? "var(--accent)" : "var(--bg)",
                  color: tab === id ? "var(--accent-fg)" : "var(--fg)",
                  fontWeight: tab === id ? 600 : 400,
                  cursor: "pointer",
                }}
              >
                {TAB_LABELS[id]}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={onClose}
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
        </div>

        <div
          id="studio-tabpanel"
          role="tabpanel"
          aria-labelledby={`studio-tab-${tab}`}
          tabIndex={0}
          style={{
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            display: "flex",
            overflow: "auto",
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
          ) : (
            <WorkflowBuilder agents={agents} runModel={runModel} onRun={onRun} />
          )}
        </div>
      </div>
    </DialogFrame>
  );
}
