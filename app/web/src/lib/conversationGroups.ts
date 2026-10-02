// Groups conversations by how recently they changed, in the server's order.
// A conversation with no readable timestamp never invents a date: when none
// carry one the list stays a single untitled group.

export interface ConversationGroup<T> {
  title: string | null;
  items: T[];
}

const GROUP_ORDER = ["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"] as const;
const DAY_MS = 24 * 60 * 60 * 1000;

function recency(iso: string | null | undefined, startOfToday: number): (typeof GROUP_ORDER)[number] | null {
  const time = iso ? Date.parse(iso) : Number.NaN;
  if (!Number.isFinite(time)) return null;
  if (time >= startOfToday) return "Today";
  if (time >= startOfToday - DAY_MS) return "Yesterday";
  if (time >= startOfToday - 7 * DAY_MS) return "Previous 7 days";
  if (time >= startOfToday - 30 * DAY_MS) return "Previous 30 days";
  return "Older";
}

export function groupConversations<T extends { createdAt?: string | null; updatedAt?: string | null }>(
  items: T[],
  now: Date = new Date(),
): ConversationGroup<T>[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const labels = items.map((item) => recency(item.updatedAt || item.createdAt, startOfToday));
  if (labels.every((label) => label === null)) {
    return items.length ? [{ title: null, items }] : [];
  }
  const groups = new Map<string, T[]>();
  items.forEach((item, index) => {
    const label = labels[index] ?? "Older";
    const group = groups.get(label) ?? [];
    group.push(item);
    groups.set(label, group);
  });
  return GROUP_ORDER.filter((label) => groups.has(label)).map((label) => ({
    title: label,
    items: groups.get(label)!,
  }));
}

export function matchesConversationQuery(title: string | null | undefined, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  return (title || "Untitled").toLocaleLowerCase().includes(needle);
}
