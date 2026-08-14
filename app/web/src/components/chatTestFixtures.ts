import type {
  InspectorSnapshot,
  LibrarySummary,
  MemoryList,
} from "@/lib/inspector";
import type {
  AttachmentCapabilities,
  ModelCatalog,
  Session,
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
