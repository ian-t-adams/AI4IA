import { FlatCompat } from "@eslint/eslintrc";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// Next.js 15 still ships `eslint-config-next` as an eslintrc-style shareable
// config (no native flat-config export yet), so FlatCompat is the
// framework-recommended bridge for consuming it from ESLint 9 flat config —
// not a legacy shim to remove. Revisit once eslint-config-next exports flat.
const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  {
    ignores: ["node_modules/**", ".next/**", "out/**", "coverage/**", "next-env.d.ts"],
  },
  // `next/core-web-vitals` already wires up react + react-hooks
  // (rules-of-hooks = error, exhaustive-deps = warn) and jsx-a11y;
  // `next/typescript` layers @typescript-eslint/recommended over the TS sources.
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default eslintConfig;
