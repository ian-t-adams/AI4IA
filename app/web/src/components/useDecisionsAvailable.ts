"use client";

import { useEffect, useState } from "react";
import { decisionModels } from "@/lib/decisions";

// Visibility follows the same authenticated options as the playground; the API
// still owns admission. Never retain another account's availability.
type Availability = "loading" | "available" | "unavailable" | "error";

export function useDecisionsAvailable(ownerKey: string | null) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ owner: string | null; attempt: number; status: Availability }>({
    owner: ownerKey, attempt, status: ownerKey === null ? "unavailable" : "loading",
  });

  // Invalidate during the owner transition, including sign-out and a return to
  // the same key, rather than allowing a previous result to match again.
  if (state.owner !== ownerKey || state.attempt !== attempt) {
    setState({ owner: ownerKey, attempt, status: ownerKey === null ? "unavailable" : "loading" });
  }

  useEffect(() => {
    if (ownerKey === null) return;
    const controller = new AbortController();
    decisionModels(controller.signal).then((models) => {
      if (!controller.signal.aborted) {
        setState({ owner: ownerKey, attempt, status: models.length > 0 ? "available" : "unavailable" });
      }
    }).catch(() => {
      if (!controller.signal.aborted) {
        setState({ owner: ownerKey, attempt, status: "error" });
      }
    });
    return () => controller.abort();
  }, [ownerKey, attempt]);

  const status: Availability = ownerKey === null ? "unavailable"
    : state.owner === ownerKey && state.attempt === attempt ? state.status : "loading";
  return { status, retry: () => setAttempt((value) => value + 1) };
}
