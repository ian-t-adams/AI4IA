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
  // True for text-chat models offered in the chat/agent pickers; false for
  // capability models (image, video, tts, transcription, embedding, rerank) and
  // voice models (realtime, audio), reached through their own surfaces/tools.
  // Mirrors the backend ModelEntry.conversational computed field.
  conversational: boolean;
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

// --- User-defined agents & workflows (Phase 8 Studio) ---

// Durable user-authored agent persona. `id === name`; `name` is immutable.
export interface UserAgent {
  id: string;
  userId: string;
  name: string;
  displayName: string;
  description: string;
  systemPrompt: string;
  defaultModel: string | null;
  tools: string[];
  links: string[];
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
}

// Create payload — carries `name`; server owns id/userId/timestamps.
export interface UserAgentCreate {
  name: string;
  displayName?: string | null;
  description: string;
  systemPrompt: string;
  defaultModel?: string | null;
  tools: string[];
  links: string[];
  enabled: boolean;
}

// Update payload — deliberately has NO `name` (the name is the immutable id/path).
export interface UserAgentUpdate {
  displayName?: string | null;
  description: string;
  systemPrompt: string;
  defaultModel?: string | null;
  tools: string[];
  links: string[];
  enabled: boolean;
}

// One step of a workflow: run `agent` with the rendered `instruction`
// (may reference the {input} and {previous} placeholders).
export interface WorkflowStep {
  agent: string;
  instruction: string;
}

export interface Workflow {
  id: string;
  userId: string;
  name: string;
  displayName: string;
  description: string;
  steps: WorkflowStep[];
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface WorkflowCreate {
  name: string;
  displayName?: string | null;
  description: string;
  steps: WorkflowStep[];
  enabled: boolean;
}

export interface WorkflowUpdate {
  displayName?: string | null;
  description: string;
  steps: WorkflowStep[];
  enabled: boolean;
}

export interface WorkflowRunResult {
  sessionId: string;
  ok: boolean;
  message: Message;
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

// --- Document upload (Phase 7C) ---

// Summary of an uploaded document (never carries the full extracted text).
export interface DocumentSummary {
  id: string;
  sessionId: string;
  filename: string;
  contentType: string;
  size: number;
  charCount: number;
  truncated: boolean;
  preview: string;
  createdAt: string;
}
