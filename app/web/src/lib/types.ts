// Shared types mirroring the FastAPI backend contract (app/api).

export type MessageRole = "system" | "user" | "assistant";
export type MessageStatus = "complete" | "streaming" | "cancelled" | "error";
// How a turn entered the conversation: typed text ("chat") or a Voice Live
// exchange persisted back into the same session ("voice"). Provenance only —
// voice turns are ordinary messages and feed model context like any other.
export type MessageSource = "chat" | "voice";

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
  // Per-model context budget metadata. Null when the catalog does not specify
  // it for a model (e.g. model-router); callers fall back to fixed defaults.
  contextWindow: number | null;
  maxOutputTokens: number | null;
  // Whether temperature/top_p actually reach the provider. False for reasoning
  // models, whose sampling params the gateway strips because they 400 on
  // non-default values. Server-computed so the UI never presents a control that
  // is silently discarded.
  supportsSampling: boolean;
  // Allowed reasoning_effort values; empty when the model does not accept it.
  reasoningEffortOptions: string[];
  options: DeploymentOption[];
}

export interface ModelCatalog {
  models: ModelEntry[];
}

export interface Session {
  id: string;
  userId: string;
  title: string;
  titleSource: "auto" | "manual";
  model: string | null;
  systemPrompt: string | null;
  agentName: string | null;
  toolOverrides: { added: string[]; removed: string[] };
  libraryDocumentIds: string[] | null;
  summaryVersion?: number;
  createdAt: string;
  updatedAt: string;
}

export interface ToolOverrides {
  added: string[];
  removed: string[];
}

export interface ConversationDraftDefaults {
  agentName: string | null;
  toolOverrides: ToolOverrides;
  libraryDocumentIds: string[];
}

export interface MessageAttachment {
  id: string;
  kind: string;
  mimeType: string;
  prompt: string | null;
  model: string | null;
  size: string | null;
  quality?: string | null;
  durationSeconds?: number | null;
  filename?: string | null;
}

// A redacted, user-facing entry in an assistant turn's activity trace: which tool
// ran and how it turned out. Streamed live during the turn (including the
// pre-execution "tool_start" marker) and persisted on the assistant message.
// Mirrors ai4ia_api.sessions.models.ActivityStep.
export interface ActivityStep {
  kind: "tool_start" | "tool_result" | "tool_denied" | "tool_error" | "delegate" | "final";
  label: string;
  tool?: string | null;
  detail?: string | null;
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
  attachments?: MessageAttachment[];
  source?: MessageSource;
  // Redacted activity trace for an agentic/tool turn; absent for plain turns.
  steps?: ActivityStep[] | null;
}

// A finalized Voice Live turn the web persists back into the shared session.
export interface VoiceTurnInput {
  role: "user" | "assistant";
  text: string;
  createdAt?: string;
}

export interface AgentSummary {
  name: string;
  displayName: string;
  description: string;
  enabled: boolean;
}

// --- User-defined agents & workflows ---

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
  // Tools granted to this step ON TOP OF whatever its agent already declares —
  // additive, never a replacement. Curated agents ship fixed tool lists a user
  // cannot edit (`general` declares only `get_current_time`), so without this a
  // step targeting one could never save a memory or run a calculation.
  // Optional on read so workflows stored before the field existed still parse.
  extraTools?: string[];
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

export interface WorkflowListResult {
  workflows: Workflow[];
  // Whether this deployment can honour a durable run. Server-derived from the
  // live orchestration client, so it never claims a capability the run endpoint
  // would reject.
  durableAvailable: boolean;
}

// 202 body: the run was SCHEDULED, not completed. The assistant message does not
// exist yet — poll getWorkflowRun(runId) until the status is terminal.
export interface WorkflowRunAccepted {
  sessionId: string;
  runId: string;
  status: string;
}

export interface WorkflowRunStatus {
  runId: string;
  status: string;
  ok?: boolean | null;
  text?: string | null;
  error?: string | null;
}

// Discriminated on the HTTP status the server actually returned rather than on
// the body's shape, so a partially-populated payload can never be mistaken for
// the other branch: a durable run reported as synchronous would tell the caller
// "done" while the orchestration is still running.
export type WorkflowRunOutcome =
  | { scheduled: false; result: WorkflowRunResult }
  | { scheduled: true; run: WorkflowRunAccepted };

export interface ChatParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  // Only meaningful for reasoning models; the allowed values come from the
  // model's reasoningEffortOptions rather than a hardcoded list here, because
  // they differ by family (GPT-5 adds "minimal", o-series rejects it).
  reasoning_effort?: string;
}

export interface ToolCatalogItem {
  name: string;
  label: string;
  description: string;
  source: string;
  risk: "safe" | "external" | "destructive" | null;
  requiresApproval: boolean | null;
  scopes: string[] | null;
  available: boolean;
  selectable: boolean;
  detail?: string | null;
  ownership: string;
  typed: boolean | null;
  voice: boolean | null;
}

export interface AttachmentCapabilities {
  ingestPath: "library" | "session";
  maxBytes: number;
  maxPerUserDocuments: number | null;
  maxPerSessionDocuments: number;
  extensions: string[];
  mimeTypes: string[];
  modalities: string[];
}

// --- Image generation ---

export interface ImageRequest {
  prompt: string;
  model?: string | null;
  size?: string | null;
  quality?: string | null;
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
  quality: string;
  images: GeneratedImageData[];
}

// Persisted custom-background selection. A preset references a named gradient;
// a generated background carries a full data URL produced by image generation.
export type BackgroundConfig =
  | { kind: "preset"; id: string }
  | { kind: "generated"; dataUrl: string };

// --- Document upload ---

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
