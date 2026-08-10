"use client";

import dynamic from "next/dynamic";
import { useLayoutEffect, useRef } from "react";

import { focusMainContent } from "./SkipLink";

function LoadingWorkspace() {
  const mainRef = useRef<HTMLElement>(null);

  useLayoutEffect(
    () => () => {
      if (document.activeElement !== mainRef.current) return;
      // React removes the loading landmark before the lazy workspace commits its
      // replacement. Defer until that replacement main is addressable.
      queueMicrotask(focusMainContent);
    },
    [],
  );

  return (
    <main ref={mainRef} id="main" aria-busy="true" aria-live="polite">
      Loading workspace…
    </main>
  );
}

const ChatApp = dynamic(
  () => import("./ChatApp").then((module) => module.ChatApp),
  {
    ssr: false,
    loading: LoadingWorkspace,
  },
);

export function AuthenticatedChatApp() {
  return <ChatApp />;
}
