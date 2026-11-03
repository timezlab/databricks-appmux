import os from "node:os";
import path from "node:path";

import { defineConfig } from "@playwright/test";

// End-to-end CSP smoke test (e2e/). The spec starts its own server on a free
// port (e2e/server.ts), so there is no `webServer` and no fixed port. The
// browser is the Chromium Playwright already finds in ~/.cache/ms-playwright;
// nothing here downloads one.
export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  // Builds the page of shadcn primitives the spec drives (e2e/primitives/).
  globalSetup: "./e2e/primitives/build.ts",
  fullyParallel: true,
  // Each worker starts a server and a browser; the default (half the cores) is
  // more than a shared machine needs for 50 short tests.
  workers: 4,
  retries: 0,
  reporter: "list",
  // Traces and failure artifacts stay out of the repository.
  outputDir: path.join(os.tmpdir(), "appmux-e2e-results"),
  use: {
    browserName: "chromium",
    headless: true,
  },
});
