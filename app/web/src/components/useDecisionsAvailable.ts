"use client";

import { useEffect, useState } from "react";
import { decisionModels } from "@/lib/decisions";

// Visibility follows the same authenticated options as the playground; the API
// still owns admission. Never retain another account's availability.
type Availability = "loading" | "available" | "unavailable" | "error";

export function useDecisionsAvailable(ownerKey: string | null) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ owner: string; attempt: number; status: Availability } | null>(null);

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
    : state?.owner === ownerKey && state.attempt === attempt ? state.status : "loading";
  return { status, retry: () => setAttempt((value) => value + 1) };
}
