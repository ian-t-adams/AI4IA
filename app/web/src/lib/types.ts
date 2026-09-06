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
  // Where processing may actually occur, derived server-side from the SKU:
  // "global" | "us" | "eu". Deliberately NOT the same as `dataZone`, which is
  // only the endpoint's geography — a GlobalStandard deployment in a Swedish
  // region is reachable from the EU but may be processed anywhere, so its
  // residency is "global". Use this, not dataZone, to state a guarantee.
  residency: string;
}

export interface ModelEntry {
  id: string;
  displayName: string;
  category: string;
  format: string;
  api?: string;
  // True for text-chat models offered in the chat/agent pickers; false for
  // capability models (image, video, tts, transcription, embedding, rerank) and
  // voice models (realtime, audio), reached through their own surfaces/tools.
  // Mirrors the backend ModelEntry.conversational computed field.
  conversational: boolean;
  // Per-model context budget metadata. Null when the catalog does not specify
  // it for a model (e.g. model-router); callers fall back to fixed defaults.
  contextWindow: number | null;
  maxOutputTokens: number | null;
  supportsTools?: boolean;
  inputModalities?: string[];
  imageSizes?: string[] | null;
  imageQualities?: string[] | null;
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
  // The deployment's active data-residency policy: "global" | "zonal" | "us" |
  // "eu". The server has already filtered `models` and each entry's `options`
  // by it, so this is for display — it lets the UI state the guarantee rather
  // than leaving a user to infer it from region names.
  residencyPolicy: string;
}

// Public, server-owned summary. Never accepted by generic session PATCH or
// workflow definitions; consent is explicitly granted for a session or one run.
export interface ToolConsentSummary {
  id: string;
  scope: "session" | "run";
  grantedAt: string;
  expiresAt: string;
  toolCount: number;
}

export type ToolConsentStatus =
  | "off"
  | "active"
  | "expired"
  | "revoked"
  | "changed"
  | "disabled"
  | "unavailable";

export type ToolApprovalSource =
  | "session"
  | "run"
  | "invocation"
  | "not_required"
  | "operator";

export interface Session {
  id: string;
  userId: string;
  title: string;
  titleSource: "auto" | "manual";
  model: string | null;
  systemPrompt: string | null;
  agentName: string | null;
  toolOverrides: { added: string[]; removed: string[] };
  toolConsent?: ToolConsentSummary | null;
  libraryDocumentIds: string[] | null;
  imagePreferences?: ImageGenerationPreferences;
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

export interface ImageGenerationPreferences {
  models: string[];
  size: string | null;
  quality: string | null;
}

export interface MessageAttachment {
  id: string;
  kind: string;
  mimeType: string;
  prompt: string | null;
  model: string | null;
  provider?: string | null;
  deployment?: string | null;
  region?: string | null;
  dataZone?: string | null;
  residency?: string | null;
  size: string | null;
  quality?: string | null;
  costKnown?: boolean | null;
  estimatedCostUsd?: number | null;
  pricingBasis?: string | null;
  priceVersion?: string | null;
  status?: string | null;
  error?: string | null;
  durationSeconds?: number | null;
  filename?: string | null;
}

// A redacted, user-facing entry in an assistant turn's activity trace: which tool
// ran and how it turned out. Streamed live during the turn (including the
// pre-execution "tool_start" marker) and persisted on the assistant message.
// Mirrors ai4ia_api.sessions.models.ActivityStep.
export interface ActivityStep {
  kind: "tool_start" | "tool_result" | "tool_denied" | "tool_error" | "delegate" | "final" | "workflow_step" | "workflow_error";
  label: string;
  tool?: string | null;
  detail?: string | null;
}

// One content-safety category's verdict for one half of the exchange. AI4IA
// runs every model under an annotate-only policy: nothing is ever blocked, so
// these verdicts are the only visible evidence the filters ran.
export interface SafetySignal {
  category: string;
  scope: "prompt" | "completion";
  // Harm categories carry a severity; detection filters (jailbreak, protected
  // material) carry `detected` instead. Exactly one is set.
  severity?: string | null;
  // Normalized rank of `severity` on a 0..SAFETY_MAX_SEVERITY_LEVEL scale, so
  // "medium" can be shown as "medium (level 2 of 3)". Null for detection
  // filters and for any provider severity outside the known scale — an
  // unrecognized value is shown as itself, never ranked against a scale it may
  // not belong to. Mirrors ai4ia_api.safety.severity_level.
  severityLevel?: number | null;
  detected?: boolean | null;
  // Whether the platform reported filtering. Expected false under the
  // annotate-only policy; true can indicate provider behavior or policy drift.
  filtered: boolean;
  // Agent/tool loops may make several model calls in one user turn.
  modelCall?: number | null;
  agent?: string | null;
}

// Whether a platform guardrail assessment exists for a turn at all. "Reported"
// and "unavailable" are different facts, and collapsing them would let a turn
// nobody assessed look exactly like a turn that came back clean.
export type SafetyStatus = "reported" | "partial" | "unavailable";

// Top of the normalized severity scale (safe=0, low=1, medium=2, high=3).
// Mirrors ai4ia_api.safety.MAX_SEVERITY_LEVEL.
export const SAFETY_MAX_SEVERITY_LEVEL = 3;

export interface MessageSafety {
  signals: SafetySignal[];
  // Absent on rows written before assessment coverage was recorded; those rows
  // carry signals, so treating a missing status as "reported" preserves their
  // original meaning exactly.
  status?: SafetyStatus;
  provider?: string | null;
  // Enforcement posture ("annotate_only"): nothing was blocked or rewritten.
  mode?: string;
  // Halves of the exchange the provider actually assessed.
  coverage?: string[];
  signalCount?: number;
  truncated?: boolean;
  errors?: string[];
}

// --- Execution receipt -------------------------------------------------------
// What was actually supplied to the model for one turn, which tools it was
// offered, and which it invoked. Mirrors ai4ia_api.receipts.
//
// Every payload here has already been through the server's credential redactor
// and is bounded; `sha256`/`bytes` describe the FULL redacted payload, so a
// truncated body still proves how large the original was. Nothing in this shape
// claims to expose model-internal reasoning — there is no field for one.
export interface ReceiptPayload {
  text: string;
  sha256: string;
  bytes: number;
  truncated: boolean;
}

export interface ReceiptPromptMessage {
  role: string;
  content: ReceiptPayload;
  toolCalls?: ReceiptPayload | null;
  toolCallId?: string | null;
}

export interface ReceiptContextBlock {
  // memory | documents | library | summary | notice
  kind: string;
  // Whether the block actually reached the model. A built-but-displaced block
  // never influenced the answer.
  admitted: boolean;
  content?: ReceiptPayload | null;
  sources?: ReceiptContextSource[];
  sourceCount?: number;
}

export interface ReceiptContextSource {
  id: string;
  version?: string | null;
  updatedAt?: string | null;
  kind?: string | null;
  documentId?: string | null;
  label?: string | null;
  contentSha256?: string | null;
  score?: number | null;
}

export interface ReceiptToolOffer {
  name: string;
  description?: string | null;
  parametersSha256: string;
}

export interface ReceiptToolCall {
  tool: string;
  // Missing on older receipts means unknown, NOT automatically approved.
  approval?: ToolApprovalSource | null;
  consentId?: string | null;
  callId?: string | null;
  // result | delegate | denied | error
  outcome: string;
  detail?: string | null;
  arguments?: ReceiptPayload | null;
  result?: ReceiptPayload | null;
}

export interface ReceiptRuntime {
  modelId?: string | null;
  deployment?: string | null;
  region?: string | null;
  sku?: string | null;
  dataZone?: string | null;
  // Processing scope, which is NOT the same claim as `dataZone`.
  residency?: string | null;
  api?: string | null;
  agent?: string | null;
  instructionSource?: string | null;
  instructionSha256?: string | null;
  agentConfigSha256?: string | null;
}

export interface ReceiptUsage {
  known: boolean;
  complete: boolean;
  calls: number;
  promptTokens?: number | null;
  completionTokens?: number | null;
  totalTokens?: number | null;
}

export interface ReceiptSafetySummary {
  status: SafetyStatus;
  provider?: string | null;
  mode?: string | null;
  coverage: string[];
  signalCount: number;
  truncated: boolean;
}

export interface ReceiptModelRequest {
  iteration: number;
  prompt: ReceiptPromptMessage[];
  promptMessageCount: number;
  promptBytes: number;
}

export interface ExecutionReceipt {
  version: number;
  correlationId?: string | null;
  runtime: ReceiptRuntime;
  prompt: ReceiptPromptMessage[];
  promptMessageCount: number;
  promptBytes: number;
  contextBlocks: ReceiptContextBlock[];
  droppedHistoryMessages: number;
  droppedContextBlocks: string[];
  toolsOffered: ReceiptToolOffer[];
  toolsOfferedCount: number;
  toolCalls: ReceiptToolCall[];
  toolCallCount: number;
  approvalsRequested: number;
  approvalsGranted: number;
  // Absent on old receipts is unknown; do not infer a count from a bounded list.
  autoApprovedToolCalls?: number;
  toolConsent?: ToolConsentSummary | null;
  usage?: ReceiptUsage;
  safety?: ReceiptSafetySummary;
  delegations?: ExecutionReceipt[];
  modelRequests?: ReceiptModelRequest[];
  iterations: number;
  // complete | incomplete | error | cancelled
  status: string;
  partial: boolean;
  truncated: boolean;
  notes: string[];
}

// --- Citation provenance (audit P1-14) --------------------------------------
// `verified` means the cited span id was in the server-minted registry for that
// turn. It does NOT mean the span supports the sentence it is attached to; the
// excerpt is carried so the reader can judge that themselves.
export type CitationStatus = "verified" | "unverified";

// One retrieval span exactly as it was injected into a turn. `documentId` is
// identity; `filename` is a display label only.
export interface RetrievedSource {
  spanId: string;
  documentId: string;
  documentVersion?: string | null;
  filename: string;
  heading?: string | null;
  charStart?: number | null;
  charEnd?: number | null;
  startMs?: number | null;
  endMs?: number | null;
  speaker?: string | null;
  excerpt: string;
  excerptTruncated?: boolean;
  contentSha256: string;
  retrievedAt: string;
  score?: number | null;
}

// One citation the answer made, and what became of it.
export interface MessageCitation {
  spanId: string;
  status: CitationStatus;
  documentId?: string | null;
  filename?: string | null;
  startMs?: number | null;
  occurrences: number;
  raw?: string | null;
}

// A tool call the server refused to execute because it needs a fresh, per-call
// human approval bound to its exact arguments (audit finding P1-13). Mirrors
// ai4ia_api.agents.approvals.PendingToolApproval.
//
// This is the *persisted* shape. It deliberately does NOT carry the grant —
// only `grantHash` — so reading a conversation never confers the ability to
// approve its outbound calls. The grant arrives once, on the SSE stream, as
// `PendingToolApprovalPrompt` below.
export interface PendingToolApproval {
  id: string;
  // Runtime dispatch identity (an opaque provider-safe alias for MCP tools).
  tool: string;
  // The durable, human-readable name to show (e.g. `mcp:courier/send`).
  label: string;
  // Destination host this call would reach. Null when the tool declares more
  // than one, in which case the UI must not imply a single destination.
  host?: string | null;
  purpose?: string;
  risk?: string;
  argumentsDigest: string;
  // Redacted, bounded, single-line view of the arguments. Server-built with the
  // same redactor the activity trace uses; never re-derive it client-side.
  argumentsPreview?: Record<string, string>;
  // Keys whose VALUE the redactor masked. `***REDACTED***` means "hidden from
  // you, but sent in full" — a materially different claim from "this is the
  // value", so the card must render the two differently.
  argumentsMasked?: string[];
  // Keys whose value was length-capped for display (value ends in "…").
  argumentsElided?: string[];
  // Count of arguments NOT shown at all. The digest covers the whole argument
  // object but the card does not, and the argument set is model-controlled — so
  // a non-zero count MUST be surfaced, or padding with filler keys becomes a
  // way to push an exfiltration's destination off the card.
  argumentsOmitted?: number;
  grantHash: string;
  consumed?: boolean;
  expiresAt: string;
  createdAt: string;
}

// The same record plus its one-time grant, delivered exactly once over SSE.
export interface PendingToolApprovalPrompt extends PendingToolApproval {
  grant: string;
}

// What the client sends back to redeem an approval. The server reads WHAT was
// approved from its own record; these two opaque strings are the entire client
// contribution, and any other field is rejected with a 422.
export interface ToolApprovalDecision {
  requestId: string;
  grant: string;
}

export interface Message {
  workflowRunId?: string | null;
  workflowRunStatus?: string | null;
  workflowToolConsent?: ToolConsentSummary | null;
  workflowConsentRevoked?: boolean;
  workflowStepReceipts?: ExecutionReceipt[] | null;
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
  // Annotate-only content-safety verdicts. `null`/absent means the provider
  // reported none, which is NOT the same as "the filters found nothing".
  safety?: MessageSafety | null;
  // Tool calls held pending a per-invocation approval on this turn.
  pendingApprovals?: PendingToolApproval[] | null;
  // Span-level citation provenance. `sources` is the server-minted registry of
  // retrieval spans injected into the turn; `citations` is what the answer
  // actually cited, each checked against it. Both `null`/absent means the turn
  // was never attested (retrieval did not run, or the row predates the feature),
  // which is deliberately distinct from an empty registry — see
  // `app/api/src/ai4ia_api/citations.py`.
  sources?: RetrievedSource[] | null;
  citations?: MessageCitation[] | null;
  // What was supplied to the model, offered to it, and invoked by it on this
  // turn. `null`/absent means the turn was never receipted (it predates the
  // feature), which is distinct from a receipt with empty lists — that one
  // asserts nothing was offered and nothing ran.
  executionReceipt?: ExecutionReceipt | null;
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

export interface WorkflowRunRequest {
  sessionId: string;
  input: string;
  model?: string | null;
  durable?: boolean;
  idempotencyKey?: string;
  // Per invocation only. Not a workflow setting or a remembered preference.
  autoApproveTools?: boolean;
}

export interface WorkflowRunResult {
  autoApproveTools?: boolean;
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
  toolAutoApproveAvailable: boolean;
}

// 202 body: `accepted` means the run was scheduled; `pending`/
// `acceptance_unknown` carries authoritative retry timing and must be recovered
// before polling DTS, where the deterministic instance may not exist yet.
export interface WorkflowRunAccepted {
  autoApproveTools?: boolean;
  toolConsent?: ToolConsentSummary | null;
  sessionId: string;
  runId: string;
  status: string;
  idempotencyKey: string;
  retryAfterSeconds?: number | null;
  leaseExpiresAt?: string | null;
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

export interface ImagePriceOption {
  size: string;
  quality: string;
  costKnown: boolean;
  estimatedCostUsd: number | null;
  pricingBasis: string | null;
}

export interface ImageModelOption {
  id: string;
  displayName: string;
  provider: string;
  sizes: string[];
  qualities: string[];
  dataZones: string[];
  residencies: string[];
  prices: ImagePriceOption[];
}

export interface ImageOptionsResponse {
  maxSelectedModels: number;
  currency: string;
  priceVersion: string | null;
  models: ImageModelOption[];
}

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
