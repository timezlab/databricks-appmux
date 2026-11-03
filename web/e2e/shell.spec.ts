/**
 * The parts of the shell that open, in a real Chromium under the reference
 * CSP: the app switcher of the top bar (open, filter, choose, Escape) and,
 * below 768 px, the sidebar's phone menu (open, choose, Escape, outside click),
 * with no sideways scroll and targets at least 40 px tall.
 *
 * `npm run build` first; `npm run test:e2e` runs this.
 */
import { expect, test } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";

import {
  NO_PROBLEMS,
  hasHorizontalOverflow,
  problemsOf,
  watchPage,
} from "./page-watch.ts";
import { PAGES, VIEWPORTS, waitForPageReady } from "./pages.ts";
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

/** The smallest height of something a thumb presses (docs/product/admin-ui-design.md, Layout). */
const MIN_TOUCH_TARGET_PX = 40;

const [DESKTOP, PHONE] = VIEWPORTS;

function pageEntry(name: string): PageEntry {
  const entry = PAGES.find((candidate) => candidate.name === name);
  if (entry === undefined) throw new Error(`pages.ts has no ${name} entry`);
  return entry;
}

async function open(page: Page, entry: PageEntry): Promise<void> {
  await page.goto(`${server.origin}${entry.path}`);
  await waitForPageReady(page, entry);
}

/**
 * The rendered heights of what `targets` matches, to a hundredth of a pixel:
 * a box laid out at a fractional position measures 39.99997 for a 40 px rule.
 */
async function heightsOf(targets: Locator): Promise<number[]> {
  const heights = await targets.evaluateAll((elements) =>
    elements.map((element) => element.getBoundingClientRect().height),
  );
  return heights.map((height) => Math.round(height * 100) / 100);
}

test.describe("the app switcher", () => {
  for (const viewport of VIEWPORTS) {
    test.describe(`${String(viewport.width)}px`, () => {
      test.use({ viewport });

      test("opens, filters as you type, goes to the same section of the chosen app, and closes on Escape", async ({
        page,
      }) => {
        const watch = await watchPage(page);
        await open(page, pageEntry("app-deployments"));
        const trigger = page.getByRole("button", { name: /^App: / });
        await expect(trigger).toHaveAttribute("aria-expanded", "false");

        await trigger.click();
        const switcher = page.getByRole("dialog", { name: "Switch app" });
        await expect(switcher.getByRole("option")).toHaveCount(3);
        // The list taught the bar the app's name.
        await expect(
          page.getByRole("button", { name: "App: Sales dashboard" }),
        ).toBeVisible();
        expect(await hasHorizontalOverflow(page)).toBe(false);

        await switcher.getByRole("combobox").fill("weekly");
        await expect(switcher.getByRole("option")).toHaveCount(1);
        await switcher.getByRole("combobox").fill("zzz");
        await expect(switcher.getByRole("option")).toHaveCount(0);
        await expect(switcher).toContainText("No app matches");

        await page.keyboard.press("Escape");
        await expect(switcher).toBeHidden();
        await expect(trigger).toHaveAttribute("aria-expanded", "false");
        await expect(trigger).toBeFocused();

        await trigger.click();
        await switcher.getByRole("combobox").fill("weekly");
        await switcher.getByRole("option", { name: /Weekly reports/ }).click();
        await expect(switcher).toBeHidden();
        await page.waitForURL(
          `${server.origin}${BASE_PATH}/app/deployments/?slug=reports`,
        );

        expect(await problemsOf(watch)).toEqual(NO_PROBLEMS);
      });
    });
  }
});

/**
 * What of `container` lies outside its box: every descendant that is drawn
 * (a screen-reader-only text is a 1 px clipped box, and is left out), named by
 * its tag and accessible text, with how far it reaches past either edge.
 */
async function spillsOf(container: Locator): Promise<string[]> {
  return container.evaluate((root) => {
    const box = root.getBoundingClientRect();
    const spills: string[] = [];
    for (const element of root.querySelectorAll("*")) {
      const rect = element.getBoundingClientRect();
      if (rect.width <= 1 || rect.height <= 1) continue;
      const past = Math.max(rect.right - box.right, box.left - rect.left);
      if (past > 0.5) {
        const name =
          element.getAttribute("aria-label") ??
          element.textContent?.trim() ??
          "";
        spills.push(
          `${element.tagName.toLowerCase()} "${name}" ${past.toFixed(1)} px`,
        );
      }
    }
    return spills;
  });
}

test.describe("the sidebar column", () => {
  test.use({ viewport: DESKTOP });

  test("keeps everything inside its box, expanded and collapsed to icons", async ({
    page,
  }) => {
    const watch = await watchPage(page);
    await open(page, pageEntry("app-environment"));
    const sections = page.getByRole("navigation", {
      name: "Sales dashboard sections",
    });
    expect(await spillsOf(sections)).toEqual([]);

    await sections
      .getByRole("button", { name: "Collapse the sidebar" })
      .click();
    await expect(
      sections.getByRole("button", { name: "Expand the sidebar" }),
    ).toBeVisible();
    expect(await spillsOf(sections)).toEqual([]);
    // Icons only, and each section is still named for a screen reader.
    await expect(
      sections.getByRole("link", { name: /^Requests\b/ }),
    ).toBeVisible();

    expect(await problemsOf(watch)).toEqual(NO_PROBLEMS);
  });
});

test.describe("the sidebar's phone menu", () => {
  test.describe(`${String(PHONE.width)}px`, () => {
    test.use({ viewport: PHONE });

    test("stands in for the column, lists the sections, goes to the one chosen and closes", async ({
      page,
    }) => {
      const watch = await watchPage(page);
      await open(page, pageEntry("app-deployments"));
      const sections = page.getByRole("navigation", {
        name: "Sales dashboard sections",
      });
      const button = sections.getByRole("button", {
        name: "Section: Deployments",
      });
      await expect(button).toBeVisible();
      await expect(button).toHaveAttribute("aria-expanded", "false");
      await expect(
        sections.getByRole("button", { name: "Collapse the sidebar" }),
      ).toBeHidden();
      await expect(sections.getByRole("link")).toHaveCount(0);
      const [buttonHeight] = await heightsOf(button);
      expect(buttonHeight).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX);

      await button.click();
      const menu = page.getByRole("dialog", {
        name: "Sales dashboard sections",
      });
      await expect(menu).toBeVisible();
      await expect(button).toHaveAttribute("aria-expanded", "true");
      await expect(menu.getByRole("link", { name: "All apps" })).toBeVisible();
      await expect(
        menu.getByRole("link", { name: "Deployments" }),
      ).toHaveAttribute("aria-current", "page");
      // The back link and the ten sections.
      const linkHeights = await heightsOf(menu.getByRole("link"));
      expect(linkHeights).toHaveLength(11);
      for (const height of linkHeights) {
        expect(height).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX);
      }
      expect(await hasHorizontalOverflow(page)).toBe(false);

      await page.keyboard.press("Escape");
      await expect(menu).toBeHidden();
      await expect(button).toBeFocused();

      await button.click();
      await expect(menu).toBeVisible();
      await page.getByRole("heading", { level: 1 }).click();
      await expect(menu).toBeHidden();

      await button.click();
      await menu.getByRole("link", { name: "Logs" }).click();
      await page.waitForURL(
        `${server.origin}${BASE_PATH}/app/logs/?slug=sales`,
      );
      await waitForPageReady(page, pageEntry("app-logs"));
      await expect(menu).toBeHidden();
      await expect(
        sections.getByRole("button", { name: "Section: Logs" }),
      ).toBeVisible();

      expect(await problemsOf(watch)).toEqual(NO_PROBLEMS);
    });
  });

  test.describe(`${String(DESKTOP.width)}px`, () => {
    test.use({ viewport: DESKTOP });

    test("is not shown: the column is", async ({ page }) => {
      await open(page, pageEntry("app-deployments"));
      const sections = page.getByRole("navigation", {
        name: "Sales dashboard sections",
      });
      await expect(
        sections.getByRole("button", { name: /^Section: / }),
      ).toBeHidden();
      await expect(
        sections.getByRole("link", { name: "Deployments" }),
      ).toBeVisible();
    });
  });
});
