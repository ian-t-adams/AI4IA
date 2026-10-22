import type { ExecutionReceipt, ResponseModelEvidence } from "./types";

export function responseModelLabel(model: ResponseModelEvidence | null | undefined): string {
  if (!model) return "Not recorded";
  switch (model.status) {
    case "reported":
      return model.modelId
        ? `${model.modelId}${model.version ? ` (${model.version})` : ""}`
        : "Not recorded";
    case "not_reported":
      return "Not reported by provider";
    case "unrecognized":
      return "Unrecognized response metadata";
    case "conflicting":
      return "Conflicting response metadata";
    default:
      return "Not recorded";
  }
}

export function finalResponseModel(receipt: ExecutionReceipt): ResponseModelEvidence | null {
  const count = receipt.runtime.modelCallCount;
  if (count == null || count < 1) return null;
  // Retained calls may be capped. The last retained entry is not necessarily
  // the final answering call, and child/delegated calls are separate evidence.
  const final = receipt.runtime.modelCalls?.find((call) => call.iteration === count);
  return final?.responseModel ?? null;
}
