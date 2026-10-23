import { apiFetch } from "./auth";
import { ApiError } from "./api";

export type DecisionQuestion = {
  name: string;
  instructions: string;
} & (
  | { type: "noul"; criteria?: Record<string, string> }
  | { type: "choice"; criteria: Record<string, string> }
  | { type: "score"; criteria: string[] }
);
export type DecisionRequest = {
  model: string;
  input: string;
  inputFormat: "text" | "json";
  questions: DecisionQuestion[];
};
export type DecisionAnswer =
  | { type: "noul"; noul: number }
  | { type: "choice"; choice: string; probabilities: Record<string, number>; confidence?: number }
  | { type: "score"; score: number; probabilities: Record<string, number>; confidence?: number };
export type DecisionResult = {
  model: string;
  reportedModel: string;
  answers: Record<string, DecisionAnswer>;
  usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; known: boolean };
  latencyMs: number;
  region: string;
  residency: string;
  costKnown: false;
  estimatedCostUsd: null;
};
export type DecisionModel = { id: string; displayName: string; residencies: string[] };

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function probability(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function text(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 128;
}
async function payload(response: Response): Promise<unknown> {
  if (!response.ok) {
    let detail = "Decision request failed. No automatic retry.";
    const raw: unknown = await response.json().catch(() => null);
    if (object(raw) && typeof raw.detail === "string") detail = raw.detail;
    throw new ApiError(response.status, detail);
  }
  return response.json();
}

export async function decisionModels(signal: AbortSignal): Promise<DecisionModel[]> {
  const raw = await payload(await apiFetch("/api/decisions/options", { signal, cache: "no-store" }));
  if (!object(raw) || !Array.isArray(raw.models) || !raw.models.every(
    (m) => object(m) && text(m.id) && text(m.displayName)
      && Array.isArray(m.residencies) && m.residencies.every(text),
  )) throw new Error("Decision availability is invalid. Reload availability.");
  return raw.models;
}

export function validateDecisionResult(raw: unknown, request: DecisionRequest): DecisionResult {
  const invalid = () => new Error("Decision returned invalid data. Work may have been billed; no automatic retry.");
  if (!object(raw) || raw.model !== request.model || !text(raw.reportedModel)
    || !text(raw.region) || !text(raw.residency) || !count(raw.latencyMs)
    || raw.costKnown !== false || raw.estimatedCostUsd !== null || !object(raw.usage)
    || !object(raw.answers) || Object.keys(raw.answers).length !== request.questions.length) throw invalid();
  const u = raw.usage;
  if (typeof u.known !== "boolean" || (u.known
    ? !count(u.inputTokens) || !count(u.outputTokens) || !count(u.totalTokens)
      || u.totalTokens !== u.inputTokens + u.outputTokens
    : u.inputTokens !== null || u.outputTokens !== null || u.totalTokens !== null)) throw invalid();
  const answers: Record<string, DecisionAnswer> = {};
  for (const question of request.questions) {
    const answer = raw.answers[question.name];
    if (!object(answer) || answer.type !== question.type) throw invalid();
    if (question.type === "noul") {
      if (!probability(answer.noul)) throw invalid();
      answers[question.name] = { type: "noul", noul: answer.noul };
      continue;
    }
    const keys = question.type === "choice" ? Object.keys(question.criteria)
      : question.criteria.map((_, i) => String(i));
    if (!object(answer.probabilities) || Object.keys(answer.probabilities).length !== keys.length) throw invalid();
    const distribution: Record<string, number> = {};
    for (const key of keys) {
      const p = answer.probabilities[key];
      if (!probability(p)) throw invalid();
      distribution[key] = p;
    }
    const values = Object.values(distribution);
    const tolerance = values.every((p) => Math.abs(p * 100 - Math.round(p * 100)) < 1e-10) ? values.length * .005 + 1e-12 : .001;
    if (Math.abs(values.reduce((a, b) => a + b, 0) - 1) > tolerance) throw invalid();
    if (answer.confidence !== undefined && !probability(answer.confidence)) throw invalid();
    const confidence = typeof answer.confidence === "number" ? answer.confidence : undefined;
    if (question.type === "choice") {
      if (typeof answer.choice !== "string" || !keys.includes(answer.choice)) throw invalid();
      answers[question.name] = { type: "choice", choice: answer.choice, probabilities: distribution, confidence };
    } else {
      if (typeof answer.score !== "number" || !Number.isFinite(answer.score)
        || answer.score < 0 || answer.score > keys.length - 1) throw invalid();
      answers[question.name] = { type: "score", score: answer.score, probabilities: distribution, confidence };
    }
  }
  return {
    model: request.model, reportedModel: raw.reportedModel, region: raw.region, residency: raw.residency,
    latencyMs: raw.latencyMs, answers, costKnown: false, estimatedCostUsd: null,
    usage: {
      known: u.known, inputTokens: typeof u.inputTokens === "number" ? u.inputTokens : null,
      outputTokens: typeof u.outputTokens === "number" ? u.outputTokens : null,
      totalTokens: typeof u.totalTokens === "number" ? u.totalTokens : null,
    },
  };
}

export async function runDecision(request: DecisionRequest, signal: AbortSignal): Promise<DecisionResult> {
  return validateDecisionResult(await payload(await apiFetch("/api/decisions/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request), signal, cache: "no-store",
  })), request);
}
