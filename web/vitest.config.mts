import { configDefaults, defineConfig } from "vitest/config";

// Three projects:
//  - unit: pure logic and the AST/CSS/CSP guards, in plain Node (fast, no DOM
//    globals to hide a bug);
//  - dom: component tests (`*.test.tsx`), in jsdom;
//  - export: guards over the built static export in `out/`, in plain Node.
//    They need `npm run build` first, so `npm test` leaves them out and
//    `npm run test:export` runs them.
// `@/` comes from tsconfig `paths`, the one place that defines it.
export default defineConfig({
  resolve: { tsconfigPaths: true },
  test: {
    globals: false, // tests import describe/it/expect explicitly
    projects: [
      {
        extends: true,
        test: {
          name: "unit",
          environment: "node",
          include: ["tests/**/*.test.ts"],
          exclude: [...configDefaults.exclude, "tests/export/**"],
        },
      },
      {
        extends: true,
        test: {
          name: "dom",
          environment: "jsdom",
          include: ["tests/**/*.test.tsx"],
          setupFiles: ["./tests/setup-dom.ts"],
        },
      },
      {
        extends: true,
        test: {
          name: "export",
          environment: "node",
          include: ["tests/export/**/*.test.ts"],
        },
      },
    ],
  },
});
