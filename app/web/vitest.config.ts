import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Test config for the web app. The default environment stays "node" so the
// pure-helper unit tests (src/lib/*.test.ts) run fast without a DOM. Component
// tests opt into a browser-like DOM with a `// @vitest-environment jsdom`
// docblock at the top of the file and use @testing-library/react. The shared
// setup file registers @testing-library/jest-dom matchers on `expect`.
export default defineConfig({
  // Use the automatic JSX runtime (react/jsx-runtime) so component test files
  // don't need an explicit `import React`. Next.js applies the same transform.
  // Vitest 4 transforms with oxc (Vite 8 dropped esbuild's `jsx` option); oxc
  // defaults to the automatic runtime, set here explicitly to keep intent clear.
  oxc: { jsx: { runtime: "automatic" } },
  test: {
    environment: "node",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
  resolve: {
    alias: {
      // Mirror the tsconfig "@/*" path alias so test imports resolve the same
      // way the Next.js bundler does.
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
