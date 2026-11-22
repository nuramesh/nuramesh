import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules", "src/generated.ts"] },
  {
    files: ["**/*.ts"],
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: { "@typescript-eslint/consistent-type-imports": "error" },
  },
);
