"use client";

import dynamic from "next/dynamic";

const ChatApp = dynamic(
  () => import("./ChatApp").then((module) => module.ChatApp),
  {
    ssr: false,
    loading: () => (
      <main id="main" aria-busy="true" aria-live="polite">
        Loading workspace…
      </main>
    ),
  },
);

export function AuthenticatedChatApp() {
  return <ChatApp />;
}
