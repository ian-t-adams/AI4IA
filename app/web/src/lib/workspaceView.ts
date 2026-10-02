// Which workspace surface is on screen. The conversation is the default; the
// destinations (document library, photo avatars, the agents & workflows studio,
// settings) are pages in the same shell, addressed by the URL hash so the
// browser's back/forward buttons and deep links work without a route change
// that would unmount the conversation (and any live voice session) behind them.

export type WorkspaceView = "chat" | "library" | "avatars" | "studio" | "settings";

const VIEW_SLUGS: Record<Exclude<WorkspaceView, "chat">, string> = {
  library: "library",
  avatars: "avatars",
  studio: "studio",
  settings: "settings",
};

export function viewFromHash(hash: string): WorkspaceView {
  const slug = hash.replace(/^#\/?/, "").split(/[/?#]/, 1)[0]?.toLowerCase() ?? "";
  for (const [view, value] of Object.entries(VIEW_SLUGS)) {
    if (value === slug) return view as WorkspaceView;
  }
  return "chat";
}

export function hashForView(view: WorkspaceView): string {
  return view === "chat" ? "" : `#/${VIEW_SLUGS[view]}`;
}

export const VIEW_TITLES: Record<WorkspaceView, string> = {
  chat: "Conversation",
  library: "Document library",
  avatars: "Photo avatars",
  studio: "Agents & workflows",
  settings: "Settings",
};
