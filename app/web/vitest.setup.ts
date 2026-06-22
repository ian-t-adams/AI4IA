// Registers @testing-library/jest-dom's custom matchers (toBeInTheDocument,
// toBeDisabled, toHaveValue, …) on Vitest's `expect`. Loaded via `setupFiles`
// in vitest.config.ts so every test file gets the matchers. The import is inert
// in a "node" environment (it only extends `expect`), so the existing pure-logic
// unit tests are unaffected; the matchers are only exercised by the jsdom
// component tests (marked with `// @vitest-environment jsdom`).
import "@testing-library/jest-dom/vitest";
