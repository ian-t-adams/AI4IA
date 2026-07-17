// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationInspector } from "./ConversationInspector";

const { updateSession } = vi.hoisted(() => ({ updateSession: vi.fn() }));

vi.mock("@/lib/api", () => ({
  listTools: vi.fn(async () => [
    {
      name: "calculator",
      label: "Calculator",
      description: "Math",
      source: "built-in",
      risk: "safe",
      requiresApproval: false,
      scopes: [],
      available: true,
      selectable: true,
    },
  ]),
  updateSession,
}));

vi.mock("@/lib/inspector", () => ({
  getInspector: vi.fn(async () => ({
    generatedAt: new Date().toISOString(),
    sessionId: "s1",
    title: "Chat",
    model: {
      id: "gpt-5.2",
      displayName: "GPT-5.2",
      contextWindow: 128000,
      maxOutputTokens: 32000,
    },
    instructions: {
      source: "session",
      editable: true,
      value: "Be concise",
      agentName: null,
    },
    agent: { name: null, displayName: null, description: null, enabled: true },
    tools: { inherited: [], added: [], removed: [], effective: [] },
    attachments: [],
    libraryDocuments: [],
    sessionUsage: {
      sessionId: "s1",
      totalRequests: 1,
      totalPromptTokens: 10,
      totalCompletionTokens: 20,
      totalTokens: 30,
      totalCostMicroUsd: 10,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
      latest: null,
    },
    monthlyUsage: {
      totalRequests: 1,
      totalTokens: 30,
      totalCostMicroUsd: 10,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
    },
    voice: {
      defaultProviderId: "azure_openai",
      enabledProviderIds: ["azure_openai"],
      applies: "next_connection",
    },
  })),
  listMemories: vi.fn(async () => ({
    status: "ok",
    supportsDelete: false,
    items: [],
    detail: null,
  })),
  getLibrarySummary: vi.fn(async () => ({
    status: "ok",
    total: 0,
    byStatus: {},
    byModality: {},
    recent: [],
    maxUploadBytes: 10,
    maxDocuments: 8,
    modalities: ["document"],
  })),
  deleteMemory: vi.fn(),
}));

afterEach(() => {
  cleanup();
  updateSession.mockReset();
});

describe("ConversationInspector", () => {
  it("exposes the section navigation and a single editable instruction source", async () => {
    const user = userEvent.setup();
    render(
      <ConversationInspector
        sessionId="s1"
        models={[]}
        agents={[]}
        selectedModel="gpt-5.2"
        onModelChange={vi.fn()}
        params={{}}
        onParamsChange={vi.fn()}
        systemPrompt="Be concise"
        onSystemPromptChange={vi.fn(async () => {})}
        onSessionUpdated={vi.fn()}
        voiceLocked={false}
        collapsed={false}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByRole("tablist", { name: "Inspector sections" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(
      await screen.findByRole("textbox", { name: "System prompt" }),
    ).toHaveValue("Be concise");
    expect(screen.queryByText("Voice settings")).toBeNull();
  });
});
