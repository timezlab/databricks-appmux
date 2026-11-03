/**
 * The theme switch in a real Chromium, under the reference CSP (ADR 0019):
 * each choice (Light, Dark, System) on each OS scheme gives the background of
 * its theme, survives a reload, and is on `<html>` before DOMContentLoaded (so
 * the page never paints in the wrong theme first). System follows the OS live;
 * with the script blocked the OS still decides, through CSS alone.
 *
 * The expected colours are read from globals.css, not copied here.
 *
 * `npm run build` first; `npm run test:e2e` runs this.
 */
import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { NO_PROBLEMS, problemsOf, watchPage } from "./page-watch.ts";
import { PAGES, waitForPageReady } from "./pages.ts";
import type { PageEntry } from "./pages.ts";
import { BASE_PATH, startServer } from "./server.ts";
import type { E2eServer } from "./server.ts";

let server: E2eServer;

test.beforeAll(async () => {
  server = await startServer();
});
test.afterAll(async () => {
  await server.close();
});

const GLOBALS_CSS = readFileSync(
  path.resolve(import.meta.dirname, "../src/app/globals.css"),
  "utf8",
);

/** `--background` of the first block that `selector` (a regular expression source, `^` a line start) opens. */
function backgroundToken(selector: string): string {
  const block = new RegExp(`${selector}\\s*\\{([^}]*)\\}`, "m").exec(
    GLOBALS_CSS,
  )?.[1];
  const hex = /--background:\s*#([0-9a-f]{6})\s*;/i.exec(block ?? "")?.[1];
  if (hex === undefined) {
    throw new Error(`globals.css has no --background in ${selector}`);
  }
  const [red, green, blue] = [0, 2, 4].map((start) =>
    Number.parseInt(hex.slice(start, start + 2), 16),
  );
  // How getComputedStyle reports an opaque colour.
  return `rgb(${String(red)}, ${String(green)}, ${String(blue)})`;
}

const LIGHT_BACKGROUND = backgroundToken("^:root");
const DARK_BACKGROUND = backgroundToken(':root\\[data-theme="dark"\\]');

const CHOICES = [
  { choice: "light", label: "Light" },
  { choice: "dark", label: "Dark" },
  { choice: "system", label: "System" },
] as const;

/** The OS schemes Chromium can emulate; "no-preference" is an OS that states none. */
const OS_SCHEMES = ["light", "dark", "no-preference"] as const;
type OsScheme = (typeof OS_SCHEMES)[number];

function expectedBackground(
  choice: (typeof CHOICES)[number]["choice"],
  os: OsScheme,
): string {
  const isDark = choice === "dark" || (choice === "system" && os === "dark");
  return isDark ? DARK_BACKGROUND : LIGHT_BACKGROUND;
}

function launcher(): PageEntry {
  const entry = PAGES.find((candidate) => candidate.name === "launcher");
  if (entry === undefined) throw new Error("pages.ts has no launcher entry");
  return entry;
}

/** Runs in the page before its scripts: what `data-theme` was when the document had been parsed. */
function recordThemeAtDomContentLoaded(): void {
  document.addEventListener("DOMContentLoaded", () => {
    (window as unknown as { __themeAtParse: string | null }).__themeAtParse =
      document.documentElement.getAttribute("data-theme");
  });
}

function themeAtDomContentLoaded(page: Page): Promise<string | null> {
  return page.evaluate(
    () =>
      (window as unknown as { __themeAtParse: string | null }).__themeAtParse,
  );
}

function bodyBackground(page: Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor);
}

async function openLauncher(page: Page): Promise<void> {
  await page.goto(`${server.origin}${launcher().path}`);
  await waitForPageReady(page, launcher());
}

/** Opens the theme menu (Radix opens it on pointerdown, which a click sends) and picks `label`. */
async function chooseTheme(page: Page, label: string): Promise<void> {
  await page.getByRole("button", { name: /^Theme: / }).click();
  await page.getByRole("menuitemradio", { name: label }).click();
  await expect(page.getByRole("menu")).toBeHidden();
}

test.describe("the theme switch", () => {
  for (const { choice, label } of CHOICES) {
    for (const os of OS_SCHEMES) {
      test(`${label} on an OS set to ${os}`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: os });
        const watch = await watchPage(page);
        await page.addInitScript(recordThemeAtDomContentLoaded);

        await openLauncher(page);
        // A first visit has nothing stored: System.
        expect(await themeAtDomContentLoaded(page)).toBe("system");

        await chooseTheme(page, label);
        await expect(page.locator("html")).toHaveAttribute(
          "data-theme",
          choice,
        );
        expect(await bodyBackground(page)).toBe(expectedBackground(choice, os));
        await expect(
          page.getByRole("button", { name: `Theme: ${label}` }),
        ).toBeVisible();

        await page.reload();
        await waitForPageReady(page, launcher());
        // Set by the head script before the body was parsed: no flash.
        expect(await themeAtDomContentLoaded(page)).toBe(choice);
        expect(await bodyBackground(page)).toBe(expectedBackground(choice, os));
        await expect(
          page.getByRole("button", { name: `Theme: ${label}` }),
        ).toBeVisible();

        expect(await problemsOf(watch)).toEqual(NO_PROBLEMS);
      });
    }
  }

  test("System follows the OS while the page is open, and a chosen theme does not", async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: "light" });
    const watch = await watchPage(page);
    await openLauncher(page);
    expect(await bodyBackground(page)).toBe(LIGHT_BACKGROUND);

    await page.emulateMedia({ colorScheme: "dark" });
    expect(await bodyBackground(page)).toBe(DARK_BACKGROUND);

    await chooseTheme(page, "Light");
    expect(await bodyBackground(page)).toBe(LIGHT_BACKGROUND);
    await page.emulateMedia({ colorScheme: "light" });
    await page.emulateMedia({ colorScheme: "dark" });
    expect(await bodyBackground(page)).toBe(LIGHT_BACKGROUND);

    expect(await problemsOf(watch)).toEqual(NO_PROBLEMS);
  });

  for (const os of ["light", "dark"] as const) {
    test(`with the script blocked, the OS still decides (${os})`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme: os });
      const watch = await watchPage(page);
      await page.route(`**${BASE_PATH}/theme.js`, (route) => route.abort());

      await openLauncher(page);
      await expect(page.locator("html")).not.toHaveAttribute("data-theme");
      expect(await bodyBackground(page)).toBe(
        os === "dark" ? DARK_BACKGROUND : LIGHT_BACKGROUND,
      );
      await expect(
        page.getByRole("button", {
          name: "Theme: System (the theme switch did not load)",
        }),
      ).toBeDisabled();

      // The one error is the request this test refused.
      const problems = await problemsOf(watch);
      expect(problems.consoleErrors).toHaveLength(1);
      expect(problems.consoleErrors[0]).toMatch(
        new RegExp(`^Failed to load resource: .*${BASE_PATH}/theme\\.js\\)$`),
      );
      expect({ ...problems, consoleErrors: [] }).toEqual(NO_PROBLEMS);
    });
  }
});
