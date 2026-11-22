import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "playwright-report", "test-results", "node_modules"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.strictTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
      reactHooks.configs.flat["recommended-latest"],
      jsxA11y.flatConfigs.strict,
    ],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: { "react-refresh": reactRefresh },
    rules: {
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
    },
  },
  {
    // shadcn/ui components are generated (scripts/shadcn-sync.mjs) and export variants too.
    files: ["src/components/ui/**/*.tsx"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**", "e2e/**", "*.config.ts"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  {
    // The client contracts are test code (they only run under vitest): they assert on seeded data.
    // So do the organization-safety tests (E19), which index into the seeded fixture states.
    files: ["src/api/contract.ts", "src/api/org-safety-contract.ts", "src/api/org-safety.test.ts", "src/org-safety/**/*.test.{ts,tsx}"],
    rules: { "@typescript-eslint/no-non-null-assertion": "off" },
  },
  {
    files: ["**/*.{js,mjs}"],
    extends: [js.configs.recommended],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
);
