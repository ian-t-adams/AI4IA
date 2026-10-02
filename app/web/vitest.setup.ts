// Registers @testing-library/jest-dom's custom matchers (toBeInTheDocument,
// toBeDisabled, toHaveValue, …) on Vitest's `expect`. Loaded via `setupFiles`
// in vitest.config.ts so every test file gets the matchers. The import is inert
// in a "node" environment (it only extends `expect`), so the existing pure-logic
// unit tests are unaffected; the matchers are only exercised by the jsdom
// component tests (marked with `// @vitest-environment jsdom`).
import "@testing-library/jest-dom/vitest";

// Workspace pages (library, avatars, studio, settings) live in the URL hash.
// Reset it after every test so one test's page never becomes the next test's
// starting view.
import { afterEach } from "vitest";

afterEach(() => {
  if (typeof window !== "undefined" && window.location.hash) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
});
