"use client";

import { useEffect } from "react";
import { installGlobalClientTelemetry } from "@/lib/clientTelemetry";

/**
 * Mounts once at the app root (see app/layout.tsx) to install the global
 * error/unhandledrejection telemetry listeners (see lib/clientTelemetry.ts).
 * Renders nothing -- this is a side-effect-only boot component, kept separate
 * from the providers so it never re-renders or gates the app tree.
 */
export function ClientTelemetryBoot(): null {
  useEffect(() => {
    installGlobalClientTelemetry();
  }, []);

  return null;
}
