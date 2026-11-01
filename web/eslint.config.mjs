import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules/", "out/", ".next/", "next-env.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  reactHooks.configs.flat.recommended,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/switch-exhaustiveness-check": "error",
    },
  },
  {
    // The schema graph is loaded by plain Node in tests/contract/csp.test.ts,
    // which resolves neither a bundler alias nor an extension-less path: relative
    // paths with a .ts extension only, and `zod` only through ./zod.ts (it sets
    // jitless before any schema is built).
    files: ["src/lib/api/schemas/**/*.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@/*"],
              message:
                "The schema graph must stay node-resolvable: use a relative path with a .ts extension.",
            },
            {
              regex: "^\\.{1,2}/(?!.*\\.ts$)",
              message:
                "Write the .ts extension: Node resolves the schema graph without a bundler.",
            },
          ],
          paths: [
            {
              name: "zod",
              allowTypeImports: true,
              message: "Import z from ./zod.ts so jitless is set first.",
            },
          ],
        },
      ],
    },
  },
  {
    files: ["src/lib/api/schemas/zod.ts"],
    rules: { "no-restricted-imports": "off" },
  },
  {
    // The edge serves the admin UI under a strict CSP (style-src 'self', FR-021):
    // a style attribute in the prerendered HTML is refused, and a raw HTML sink
    // is how server text becomes markup. A rule that fires in the editor is
    // cheaper than the build-time guards in tests/csp and tests/export, which
    // stay as the backstop (they also see a `style` that arrives through props).
    files: ["src/**/*.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='style']",
          message:
            "No style attribute: the CSP (style-src 'self') refuses it. Use a Tailwind class, or a native element that needs none (meter, progress).",
        },
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "No raw HTML: render the value as text so React escapes it.",
        },
      ],
    },
  },
  // Config files are plain JavaScript and outside the TypeScript project.
  { files: ["**/*.mjs"], ...tseslint.configs.disableTypeChecked },
  {
    files: ["**/*.mjs"],
    languageOptions: { globals: { process: "readonly" } },
  },
  prettier, // last: silences the rules Prettier owns
);
