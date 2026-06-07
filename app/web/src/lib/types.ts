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
  createdAt: string;
}

export interface ChatParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
}
