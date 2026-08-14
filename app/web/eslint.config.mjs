import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// eslint-config-next 16 ships native ESLint flat-config arrays (Linter.Config[])
// via its `/core-web-vitals` and `/typescript` subpath exports, so we spread them
// directly. This replaces the previous `@eslint/eslintrc` FlatCompat bridge, which
// throws "Converting circular structure to JSON" against v16's flat config.
const eslintConfig = [
  {
    ignores: ["node_modules/**", ".next/**", "out/**", "coverage/**", "next-env.d.ts"],
  },
  // `core-web-vitals` bundles the base Next config (react, react-hooks, jsx-a11y,
  // import) plus the Core Web Vitals rules; `typescript` layers
  // @typescript-eslint/recommended over the TS sources.
  ...nextCoreWebVitals,
  ...nextTypescript,
  {
    // Pin the React version for eslint-plugin-react instead of relying on its
    // "detect" mode. eslint-config-next sets `settings.react.version = "detect"`,
    // which routes the bundled eslint-plugin-react@7.37.5 through
    // `resolveBasedir` -> `context.getFilename()`. ESLint 10 removed the
    // long-deprecated `context.getFilename()` (use `context.filename`), so
    // detection throws "contextOrFilename.getFilename is not a function". An
    // explicit version skips detection entirely, keeping this bump config-only.
    settings: {
      react: {
        version: "19.0",
      },
    },
    // Keep the React Compiler static-analysis contract explicit and blocking.
    // The app has been brought into compliance, so regressions in effect state,
    // ref access, immutability, or manual memoization now fail CI immediately.
    rules: {
      "react-hooks/set-state-in-effect": "error",
      "react-hooks/refs": "error",
      "react-hooks/immutability": "error",
      "react-hooks/preserve-manual-memoization": "error",
    },
  },
];

export default eslintConfig;
