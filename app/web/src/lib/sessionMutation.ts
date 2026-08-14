import type { Message } from "./types";

export const RECONCILIATION_DELAYS_MS = [250, 750, 1500] as const;
export const UNKNOWN_STREAM_OUTCOME =
  "Outcome unknown; refresh or leave and reselect the conversation to reconcile.";

export function isCurrentSessionGeneration(
  capturedSession: string | null,
  capturedGeneration: number,
  currentSession: string | null,
  currentGeneration: number,
): boolean {
  return (
    capturedSession === currentSession &&
    capturedGeneration === currentGeneration
  );
}

export function reconcileMessages(
  previous: Message[],
  fresh: Message[],
  removeIds: ReadonlySet<string> = new Set(),
): Message[] {
  const previousById = new Map(previous.map((message) => [message.id, message]));
  const byId = new Map<string, Message>(
    fresh.map((message) => {
      const existing = previousById.get(message.id);
      if (
        existing &&
        existing.status !== "streaming" &&
        message.status === "streaming"
      ) {
        return [message.id, existing] as [string, Message];
      }
      return [message.id, message] as [string, Message];
    }),
  );
  for (const message of previous) {
    if (!removeIds.has(message.id) && !byId.has(message.id)) {
      byId.set(message.id, message);
    }
  }
  return [...byId.values()].sort(
    (left, right) =>
      Date.parse(left.createdAt) - Date.parse(right.createdAt) ||
      left.id.localeCompare(right.id),
  );
}

export function replaceTemporaryMessageId(
  messages: Message[],
  temporaryId: string,
  durableId: string,
): Message[] {
  const temporary = messages.find((message) => message.id === temporaryId);
  if (!temporary) return messages;
  const durable = messages.find((message) => message.id === durableId);
  const replacement =
    durable?.role === "assistant" &&
    durable.status === "streaming" &&
    temporary.status !== "streaming"
      ? { ...temporary, id: durableId }
      : (durable ?? { ...temporary, id: durableId });
  return [
    ...messages.filter(
      (message) => message.id !== temporaryId && message.id !== durableId,
    ),
    replacement,
  ];
}

export function terminalMessage(messages: Message[], id: string): boolean {
  const message = messages.find((candidate) => candidate.id === id);
  return Boolean(message && message.status !== "streaming");
}
