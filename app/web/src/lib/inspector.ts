import type {
  AgentSummary,
  DocumentSummary,
  Session,
  ToolCatalogItem,
} from "./types";
import type { LibraryDocument } from "./library";
import { apiFetch } from "./auth";

async function jsonOrThrow<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(`${response.status}: ${body?.detail ?? response.statusText}`);
  }
  return (await response.json()) as T;
}

export interface UsageRecordView {
  provider: string;
  model: string;
  agent: string | null;
  usageKnown: boolean;
  usageComplete: boolean;
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  costKnown: boolean;
  estCostMicroUsd: number | null;
  createdAt: string;
}

export interface SessionUsageView {
  sessionId: string;
  totalRequests: number;
  totalPromptTokens: number;
  totalCompletionTokens: number;
  totalTokens: number;
  totalCostMicroUsd: number;
  unknownUsageRequests: number;
  costUnknownRequests: number;
  latest: UsageRecordView | null;
  truncated: boolean;
  coveredRequests: number;
  coverageStart: string | null;
  coverageEnd: string | null;
}

export interface UsageSummaryView {
  totalRequests: number;
  totalTokens: number;
  totalCostMicroUsd: number;
  unknownUsageRequests: number;
  costUnknownRequests: number;
}

export interface InspectorSnapshot {
  generatedAt: string;
  sessionId: string;
  title: string;
  model: {
    id: string | null;
    displayName: string | null;
    contextWindow: number | null;
    maxOutputTokens: number | null;
  };
  instructions: {
    source: "agent" | "session" | "default";
    editable: boolean;
    value: string | null;
    agentName: string | null;
  };
  agent: Omit<AgentSummary, "name" | "displayName" | "description"> & {
    name: string | null;
    displayName: string | null;
    description: string | null;
  };
  tools: {
    inherited: string[];
    added: string[];
    removed: string[];
    effective: string[];
    voiceEffective: string[];
  };
  attachments: DocumentSummary[];
  libraryDocuments: LibraryDocument[];
  librarySelectionMode: "legacy_all" | "explicit";
  sessionUsage: SessionUsageView;
  monthlyUsage: UsageSummaryView;
  voice: {
    defaultProviderId: string | null;
    enabledProviderIds: string[];
    applies: "next_connection";
  };
}

export interface MemoryItem {
  id: string;
  text: string;
  source: string;
  sessionId: string | null;
  documentId: string | null;
  createdAt: string | null;
}

export interface MemoryList {
  status: "ok" | "disabled" | "unavailable";
  supportsDelete: boolean;
  items: MemoryItem[];
  detail: string | null;
}

export interface LibrarySummary {
  status: string;
  total: number;
  byStatus: Record<string, number>;
  byModality: Record<string, number>;
  recent: LibraryDocument[];
  maxUploadBytes: number;
  maxDocuments: number;
  modalities: string[];
}

export async function getInspector(sessionId: string): Promise<InspectorSnapshot> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/inspector`, {
      cache: "no-store",
    }),
  );
}

export async function listMemories(): Promise<MemoryList> {
  return jsonOrThrow(await apiFetch("/api/memories", { cache: "no-store" }));
}

export async function deleteMemory(memoryId: string): Promise<void> {
  const response = await apiFetch(`/api/memories/${encodeURIComponent(memoryId)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(`${response.status}: failed to delete memory`);
}

export async function getLibrarySummary(): Promise<LibrarySummary> {
  return jsonOrThrow(await apiFetch("/api/library/summary", { cache: "no-store" }));
}

export type { Session, ToolCatalogItem };
