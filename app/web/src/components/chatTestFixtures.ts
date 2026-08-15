import { vi } from "vitest";
import type {
  InspectorSnapshot,
  LibrarySummary,
  MemoryList,
} from "@/lib/inspector";
import type {
  AttachmentCapabilities,
  ModelCatalog,
  Session,
  ToolCatalogItem,
} from "@/lib/types";

export const CHAT_MODEL_CATALOG: ModelCatalog = {
  residencyPolicy: "global",
  models: [
    {
      id: "gpt-5.2",
      displayName: "GPT-5.2",
      category: "chat",
      format: "openai",
      conversational: true,
      contextWindow: 128000,
      maxOutputTokens: 32000,
      supportsSampling: true,
      reasoningEffortOptions: [],
      options: [],
    },
  ],
};

export const CHAT_ATTACHMENT_CAPABILITIES: AttachmentCapabilities = {
  ingestPath: "library",
  maxBytes: 1_000_000,
  maxPerUserDocuments: 100,
  maxPerSessionDocuments: 20,
  extensions: [".pdf"],
  mimeTypes: ["application/pdf"],
  modalities: ["document"],
};

export const DISABLED_MEMORY: MemoryList = {
  status: "disabled",
  supportsCreate: false,
  supportsEdit: false,
  supportsDelete: false,
  items: [],
  detail: "Memory disabled",
};

export function makeChatSession(id: string): Session {
  return {
    id,
    userId: "u1",
    title: `Session ${id}`,
    titleSource: "auto",
    model: "gpt-5.2",
    systemPrompt: null,
    agentName: null,
    toolOverrides: { added: [], removed: [] },
    libraryDocumentIds: [],
    createdAt: "",
    updatedAt: "",
  };
}

export function makeInspectorSnapshot(
  id: string,
  {
    title = `Session ${id}`,
    prompt = "",
    totalRequests = 0,
    promptTokens = 0,
    completionTokens = 0,
    totalTokens = 0,
    totalCostMicroUsd = 0,
    defaultProviderId = null,
    enabledProviderIds = [],
  }: {
    title?: string;
    prompt?: string;
    totalRequests?: number;
    promptTokens?: number;
    completionTokens?: number;
    totalTokens?: number;
    totalCostMicroUsd?: number;
    defaultProviderId?: string | null;
    enabledProviderIds?: string[];
  } = {},
): InspectorSnapshot {
  return {
    generatedAt: new Date().toISOString(),
    sessionId: id,
    title,
    model: {
      id: "gpt-5.2",
      displayName: "GPT-5.2",
      contextWindow: 128000,
      maxOutputTokens: 32000,
    },
    instructions: {
      source: "session",
      editable: true,
      value: prompt,
      agentName: null,
      agentSource: null,
    },
    agent: {
      name: null,
      displayName: null,
      description: null,
      enabled: true,
    },
    tools: {
      inherited: [],
      added: [],
      removed: [],
      effective: [],
      voiceEffective: [],
    },
    imagePreferences: { models: [], size: null, quality: null },
    attachments: [],
    libraryDocuments: [],
    librarySelectionMode: "explicit",
    sessionUsage: {
      sessionId: id,
      totalRequests,
      totalPromptTokens: promptTokens,
      totalCompletionTokens: completionTokens,
      totalTokens,
      totalCostMicroUsd,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
      latest: null,
      truncated: false,
      coveredRequests: totalRequests,
      coverageStart: null,
      coverageEnd: null,
    },
    monthlyUsage: {
      totalRequests,
      totalTokens,
      totalCostMicroUsd,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
    },
    voice: {
      defaultProviderId,
      enabledProviderIds,
      applies: "next_connection",
    },
  };
}

export function emptyLibrarySummary(): LibrarySummary {
  return {
    generatedAt: new Date().toISOString(),
    status: "ok",
    total: 0,
    byStatus: {},
    byModality: {},
    recent: [],
    maxUploadBytes: 100,
    maxDocuments: 100,
    modalities: ["document"],
  };
}

// ---------------------------------------------------------------------------
// Shared mock factory and reset helper for ChatApp test files.
//
// `createChatAppMocks()` documents the common mock shape. It cannot be called
// from inside a `vi.hoisted()` factory (static imports aren't available there),
// but it IS callable from `beforeEach` or any test body after module setup.
//
// `resetChatAppMocks(mocks)` wires up the standard beforeEach defaults for all
// keys both ChatApp test files share, reducing ~17 repetitive setup lines to
// a single call in each file.
// ---------------------------------------------------------------------------

/** Minimum mock shape required by `resetChatAppMocks`. */
export interface ChatAppCommonMocks {
  listModels: ReturnType<typeof vi.fn>;
  listSessions: ReturnType<typeof vi.fn>;
  listAgents: ReturnType<typeof vi.fn>;
  getAttachmentCapabilities: ReturnType<typeof vi.fn>;
  listMessages: ReturnType<typeof vi.fn>;
  listDocuments: ReturnType<typeof vi.fn>;
  listLibraryDocuments: ReturnType<typeof vi.fn>;
  listSharedWithMe: ReturnType<typeof vi.fn>;
  createSession: ReturnType<typeof vi.fn>;
  streamChat: ReturnType<typeof vi.fn>;
  appendVoiceTurns: ReturnType<typeof vi.fn>;
  toolCatalog: ToolCatalogItem[];
  getToolCatalog: ReturnType<typeof vi.fn>;
  updateSession: ReturnType<typeof vi.fn>;
  getInspector: ReturnType<typeof vi.fn>;
  listMemories: ReturnType<typeof vi.fn>;
  getLibrarySummary: ReturnType<typeof vi.fn>;
  createMemory: ReturnType<typeof vi.fn>;
  updateMemory: ReturnType<typeof vi.fn>;
  deleteMemory: ReturnType<typeof vi.fn>;
}

/** Creates a fresh set of vi.fn() stubs for the common ChatApp mock keys. */
export function createChatAppMocks(): ChatAppCommonMocks {
  return {
    listModels: vi.fn(),
    listSessions: vi.fn(),
    listAgents: vi.fn(),
    getAttachmentCapabilities: vi.fn(),
    listMessages: vi.fn(),
    listDocuments: vi.fn(),
    listLibraryDocuments: vi.fn(),
    listSharedWithMe: vi.fn(),
    createSession: vi.fn(),
    streamChat: vi.fn(),
    appendVoiceTurns: vi.fn(),
    toolCatalog: [] as ToolCatalogItem[],
    getToolCatalog: vi.fn(),
    updateSession: vi.fn(),
    getInspector: vi.fn(),
    listMemories: vi.fn(),
    getLibrarySummary: vi.fn(),
    createMemory: vi.fn(),
    updateMemory: vi.fn(),
    deleteMemory: vi.fn(),
  };
}

/**
 * Wires up the standard beforeEach defaults shared by all ChatApp test files.
 * Call this at the start of each `beforeEach`, then add file-specific setup.
 */
export function resetChatAppMocks(mocks: ChatAppCommonMocks): void {
  const sessions = [makeChatSession("A"), makeChatSession("B")];
  mocks.listModels.mockResolvedValue(CHAT_MODEL_CATALOG);
  mocks.listSessions.mockResolvedValue(sessions);
  mocks.listAgents.mockResolvedValue([]);
  mocks.getAttachmentCapabilities.mockResolvedValue(CHAT_ATTACHMENT_CAPABILITIES);
  mocks.listMessages.mockResolvedValue([]);
  mocks.listDocuments.mockResolvedValue([]);
  mocks.listLibraryDocuments.mockResolvedValue([]);
  mocks.listSharedWithMe.mockResolvedValue([]);
  mocks.createSession.mockImplementation(async (value: object) => ({
    ...makeChatSession("C"),
    ...value,
  }));
  mocks.streamChat.mockReturnValue(vi.fn());
  mocks.appendVoiceTurns.mockResolvedValue([]);
  mocks.toolCatalog = [];
  mocks.getToolCatalog.mockImplementation(async () => ({
    tools: mocks.toolCatalog,
    inheritedTools: [],
  }));
  mocks.updateSession.mockImplementation(async (id: string, value: object) => ({
    ...makeChatSession(id),
    ...value,
  }));
  mocks.getInspector.mockImplementation(async (id: string) =>
    makeInspectorSnapshot(id),
  );
  mocks.listMemories.mockResolvedValue(DISABLED_MEMORY);
  mocks.getLibrarySummary.mockResolvedValue(emptyLibrarySummary());
}
