"use client";

import { useState } from "react";
import type { AgentSummary, ModelEntry } from "@/lib/types";
import { AgentBuilder } from "./AgentBuilder";
import { WorkflowBuilder } from "./WorkflowBuilder";
import { McpServerBuilder } from "./McpServerBuilder";
import { useModalFocus } from "./useModalFocus";

type Tab = "agents" | "workflows" | "tools";

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
  const modal = useModalFocus(onClose);

  const tabBtn = (id: Tab): React.CSSProperties => ({
    padding: "8px 16px",
    borderRadius: 8,
    border: "1px solid var(--border)",
    background: tab === id ? "var(--accent)" : "var(--bg)",
    color: tab === id ? "var(--accent-fg)" : "var(--fg)",
    fontWeight: tab === id ? 600 : 400,
    cursor: "pointer",
  });

  return (
    <div
      ref={modal.ref}
      onKeyDown={modal.onKeyDown}
      role="dialog"
      aria-label="Agents and workflows builder"
      aria-modal="true"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(880px, 95vw)",
          height: "min(680px, 90vh)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => setTab("agents")} aria-pressed={tab === "agents"} style={tabBtn("agents")}>
              Agents
            </button>
            <button onClick={() => setTab("workflows")} aria-pressed={tab === "workflows"} style={tabBtn("workflows")}>
              Workflows
            </button>
            {customToolsEnabled && (
              <button onClick={() => setTab("tools")} aria-pressed={tab === "tools"} style={tabBtn("tools")}>
                Custom tools
              </button>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close builder"
            style={{ border: "none", background: "transparent", color: "var(--fg)", fontSize: "1.2em", cursor: "pointer" }}
          >
            ✕
          </button>
        </div>

        <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
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
    </div>
  );
}
