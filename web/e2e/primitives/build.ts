/**
 * Playwright's global setup: builds the primitives page (primitives-page.tsx)
 * with Vite into a fresh temporary folder and prerenders it, so server.ts can
 * serve it under the reference CSP. The page is test tooling and never part of
 * the export in `out/`.
 *
 *  - `site/index.html` is the page: Vite's client build with the server HTML
 *    of `renderPrimitivesPage()` in `#root`, hydrated by entry-client.tsx.
 *  - `site/control/index.html` is the negative control: Radix's own Tabs and
 *    ToggleGroup, prerendered with no script, whose style attributes the
 *    browser must refuse.
 *
 * The folder's path reaches the workers in `PRIMITIVES_DIR_ENV`; the returned
 * teardown removes the folder.
 */
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { build } from "vite";
import type { InlineConfig } from "vite";

import { PRIMITIVES_DIR_ENV } from "../server.ts";

const PAGE_DIR = import.meta.dirname;
const WEB_DIR = path.resolve(PAGE_DIR, "../..");
/** Where Vite's index.html takes the server HTML (index.html in this folder). */
const SERVER_HTML_MARK = "<!--primitives-->";

interface ServerEntry {
  renderPrimitivesPage: () => string;
  renderRadixControl: () => string;
}

function isServerEntry(value: unknown): value is ServerEntry {
  return (
    typeof value === "object" &&
    value !== null &&
    "renderPrimitivesPage" in value &&
    typeof value.renderPrimitivesPage === "function" &&
    "renderRadixControl" in value &&
    typeof value.renderRadixControl === "function"
  );
}

function viteConfig(outDir: string): InlineConfig {
  return {
    configFile: false,
    root: PAGE_DIR,
    base: "./",
    logLevel: "warn",
    resolve: { tsconfigPaths: true },
    // web/postcss.config.mjs: the Tailwind build the export uses.
    css: { postcss: WEB_DIR },
    build: {
      outDir,
      emptyOutDir: true,
      // Every asset is a file the CSP's 'self' allows, never a data: URL.
      assetsInlineLimit: 0,
      rolldownOptions: {
        onwarn(warning, defaultHandler) {
          // "use client" marks a Next.js client module; one bundle has no
          // server/client split for the directive to keep.
          if (warning.code === "MODULE_LEVEL_DIRECTIVE") return;
          defaultHandler(warning);
        },
      },
    },
  };
}

function controlShell(body: string): string {
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Control: Radix as shipped</title></head><body>${body}</body></html>`;
}

export default async function buildPrimitivesPage(): Promise<
  () => Promise<void>
> {
  const folder = await mkdtemp(
    path.join(os.tmpdir(), "appmux-e2e-primitives-"),
  );
  const siteDir = path.join(folder, "site");
  const serverDir = path.join(folder, "server");

  await build(viteConfig(siteDir));
  const serverConfig = viteConfig(serverDir);
  await build({
    ...serverConfig,
    build: {
      ...serverConfig.build,
      ssr: path.join(PAGE_DIR, "entry-server.tsx"),
    },
    // Bundle the dependencies too, so the output runs from the temporary
    // folder, where there is no node_modules to resolve them from.
    ssr: { noExternal: true },
  });
  await writeFile(
    path.join(serverDir, "package.json"),
    JSON.stringify({ type: "module" }),
  );

  const serverEntry: unknown = await import(
    pathToFileURL(path.join(serverDir, "entry-server.js")).href
  );
  if (!isServerEntry(serverEntry)) {
    throw new Error("the primitives server build lacks its render functions");
  }

  const indexFile = path.join(siteDir, "index.html");
  const template = await readFile(indexFile, "utf8");
  if (!template.includes(SERVER_HTML_MARK)) {
    throw new Error(`Vite's index.html lost the ${SERVER_HTML_MARK} mark`);
  }
  await writeFile(
    indexFile,
    template.replace(SERVER_HTML_MARK, () =>
      serverEntry.renderPrimitivesPage(),
    ),
  );
  await mkdir(path.join(siteDir, "control"));
  await writeFile(
    path.join(siteDir, "control", "index.html"),
    controlShell(serverEntry.renderRadixControl()),
  );

  process.env[PRIMITIVES_DIR_ENV] = siteDir;
  return async () => {
    await rm(folder, { recursive: true, force: true });
  };
}
