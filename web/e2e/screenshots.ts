/**
 * Writes `<page>-<width>-<scheme>.png` for every page (and the 404 page) at 1280 and 390 px,
 * light and dark, from the built export served the way the edge serves it.
 *
 *   npm run build
 *   node e2e/screenshots.ts <outDir> [light|dark|system]
 *
 * `<scheme>` is the OS's. With nothing stored the page follows it (System, the
 * default); the optional theme is stored as the choice before each page loads
 * (ADR 0019) and ends each file name: `<page>-<width>-<scheme>-<theme>.png`.
 *
 * The directory is an argument and has no default, so the images never land in
 * the repository by accident.
 */
import { mkdir } from "node:fs/promises";
import path from "node:path";

import { chromium } from "@playwright/test";

import {
  COLOR_SCHEMES,
  NOT_FOUND_PAGE,
  PAGES,
  VIEWPORTS,
  applyScenario,
  waitForPageReady,
} from "./pages.ts";
import { startServer } from "./server.ts";

const REPOSITORY_ROOT = path.resolve(import.meta.dirname, "../..");

function parseOutDir(argv: readonly string[]): string {
  const [, , outDirArgument] = argv;
  if (outDirArgument === undefined || outDirArgument === "") {
    console.error(
      "usage: node e2e/screenshots.ts <outDir> [light|dark|system]",
    );
    process.exit(2);
  }
  const outDir = path.resolve(outDirArgument);
  const relative = path.relative(REPOSITORY_ROOT, outDir);
  if (!relative.startsWith("..") && !path.isAbsolute(relative)) {
    console.error(
      `refusing to write screenshots inside the repository (${outDir}); pass a directory outside it`,
    );
    process.exit(2);
  }
  return outDir;
}

const THEME_CHOICES = ["light", "dark", "system"] as const;
type ThemeChoice = (typeof THEME_CHOICES)[number];
/** public/theme.js's `storageKey`; each page is checked to have applied the theme, so a stale key fails loudly. */
const THEME_STORAGE_KEY = "appmux.theme";

function parseTheme(argv: readonly string[]): ThemeChoice | undefined {
  const [, , , themeArgument] = argv;
  if (themeArgument === undefined) return undefined;
  const theme = THEME_CHOICES.find((choice) => choice === themeArgument);
  if (theme === undefined) {
    console.error(
      `unknown theme ${JSON.stringify(themeArgument)}; pass one of ${THEME_CHOICES.join(", ")}`,
    );
    process.exit(2);
  }
  return theme;
}

/** Runs in the page before its scripts; Playwright sends its source, so it uses nothing from outside. */
function storeTheme({ key, theme }: { key: string; theme: string }): void {
  try {
    window.localStorage.setItem(key, theme);
  } catch {
    // Storage refused: the page follows the OS, and the picture shows that.
  }
}

const outDir = parseOutDir(process.argv);
const theme = parseTheme(process.argv);
await mkdir(outDir, { recursive: true });

const server = await startServer();
const browser = await chromium.launch();
try {
  for (const colorScheme of COLOR_SCHEMES) {
    for (const viewport of VIEWPORTS) {
      const context = await browser.newContext({ viewport, colorScheme });
      if (theme !== undefined) {
        await context.addInitScript(storeTheme, {
          key: THEME_STORAGE_KEY,
          theme,
        });
      }
      for (const entry of [...PAGES, NOT_FOUND_PAGE]) {
        const page = await context.newPage();
        await applyScenario(page, entry);
        await page.goto(`${server.origin}${entry.path}`);
        await waitForPageReady(page, entry);
        const appliedTheme = await page.evaluate(() =>
          document.documentElement.getAttribute("data-theme"),
        );
        if (theme !== undefined && appliedTheme !== theme) {
          throw new Error(
            `${entry.name}: asked for the ${theme} theme, the page shows ${String(appliedTheme)}`,
          );
        }
        const file = path.join(
          outDir,
          `${entry.name}-${viewport.width}-${colorScheme}${theme === undefined ? "" : `-${theme}`}.png`,
        );
        if (entry.act !== undefined) {
          // A page in an edited state has a bar pinned to the bottom of the
          // screen: the first screen as a person sees it, bar over content.
          await page.evaluate(() => {
            window.scrollTo(0, 0);
          });
          const firstScreen = file.replace(/\.png$/, "-first-screen.png");
          await page.screenshot({ path: firstScreen });
          console.log(firstScreen);
        }
        // Not `fullPage`: that keeps the window as it is, so a bar pinned to
        // the bottom of the window would float over the middle of the page.
        // A window as tall as the page puts it where it ends up.
        const pageHeight = await page.evaluate(
          () => document.documentElement.scrollHeight,
        );
        await page.setViewportSize({
          width: viewport.width,
          height: Math.max(pageHeight, viewport.height),
        });
        await page.screenshot({ path: file });
        console.log(file);
        await page.close();
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
  await server.close();
}
