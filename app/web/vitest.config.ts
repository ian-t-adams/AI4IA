import { defineConfig } from "vitest/config";

// Minimal unit-test config for the web app's pure helpers (no DOM/RTL). The custom
// tools UI keeps its logic in plain functions (src/lib/customTools.ts) so they can be
// covered here without a browser environment.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
