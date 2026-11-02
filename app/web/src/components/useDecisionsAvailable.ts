"use client";

import { useEffect, useState } from "react";
import { decisionModels } from "@/lib/decisions";

// Visibility follows the same authenticated options as the playground; the API
// still owns admission. Never retain another account's availability.
export function useDecisionsAvailable(ownerKey: string | null): boolean {
  const [state, setState] = useState<{ owner: string; available: boolean } | null>(null);

  useEffect(() => {
    if (ownerKey === null) return;
    const controller = new AbortController();
    decisionModels(controller.signal).then((models) => {
      if (!controller.signal.aborted) {
        setState({ owner: ownerKey, available: models.length > 0 });
      }
    }).catch(() => {
      if (!controller.signal.aborted) {
        console.warn("Decision navigation availability could not be loaded. Open #/decisions to reload availability.");
        setState({ owner: ownerKey, available: false });
      }
    });
    return () => controller.abort();
  }, [ownerKey]);

  return ownerKey !== null && state?.owner === ownerKey && state.available;
}
