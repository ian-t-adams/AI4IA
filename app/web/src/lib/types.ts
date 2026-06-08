// Shared types mirroring the FastAPI backend contract (app/api).

export type MessageRole = "system" | "user" | "assistant";
export type MessageStatus = "complete" | "streaming" | "cancelled" | "error";

export interface DeploymentOption {
  region: string;
  dataZone: string | null;
  sku: string;
  deploymentName: string;
}

export interface ModelEntry {
  id: string;
  displayName: string;
  category: string;
  format: string;
  options: DeploymentOption[];
}

export interface ModelCatalog {
  models: ModelEntry[];
}

export interface Session {
  id: string;
  userId: string;
  title: string;
  model: string | null;
  systemPrompt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface Message {
  id: string;
  sessionId: string;
  userId: string;
  role: MessageRole;
  content: string;
  status: MessageStatus;
  model: string | null;
  agent: string | null;
  createdAt: string;
}

export interface AgentSummary {
  name: string;
  displayName: string;
  description: string;
  enabled: boolean;
}

export interface ChatParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
}

// --- Image generation (Phase 7A) ---

export interface ImageRequest {
  prompt: string;
  model?: string | null;
  size?: string | null;
  n?: number;
  region?: string | null;
  dataZone?: string | null;
}

export interface GeneratedImageData {
  b64: string;
}

export interface ImageResponse {
  model: string;
  deployment: string;
  size: string;
  images: GeneratedImageData[];
}

// Persisted custom-background selection. A preset references a named gradient;
// a generated background carries a full data URL produced by image generation.
export type BackgroundConfig =
  | { kind: "preset"; id: string }
  | { kind: "generated"; dataUrl: string };
