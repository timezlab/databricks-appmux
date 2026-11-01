/**
 * Loads every page of the built export in a real Chromium, served the way the
 * edge will serve it (e2e/server.ts), and fails on a CSP violation, a console
 * error, a page error, an empty page or a horizontal overflow.
 *
 * `npm run build` first; `npm run test:e2e` runs this.
 */
import { createHash } from "node:crypto";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import {
  COLOR_SCHEMES,
  NOT_FOUND_PAGE,
  PAGES,
  VIEWPORTS,
  applyScenario,
  waitForPageReady,
} from "./pages.ts";
import type { PageEntry } from "./pages.ts";
import { FAILING_SETTING_KEY } from "./fixtures.ts";
import { CONTROL_PREFIX, startServer } from "./server.ts";
import type { E2eServer } from "./server.ts";

let server: E2eServer;

// One server per worker: tests in a worker run one after another, so
// `server.apiCalls` can be read per test.
test.beforeAll(async () => {
  server = await startServer();
});
test.afterAll(async () => {
  await server.close();
});

// ------------------------------------------------------------ observation

interface CspViolation {
  effectiveDirective: string;
  blockedURI: string;
  sourceFile: string;
  lineNumber: number;
  columnNumber: number;
  sample: string;
  disposition: string;
  /** The element the browser refused, or "document". The policy sends no `'report-sample'`, so this is how an inline violation is told apart. */
  target: string;
}

interface ConsoleError {
  text: string;
  url: string;
}

interface Observation {
  violations: () => Promise<CspViolation[]>;
  consoleErrors: ConsoleError[];
  pageErrors: string[];
}

/** Runs in the page, before any of its scripts (`addInitScript`): Playwright sends its source text, so it must not use anything defined outside it. */
function installViolationListener(): void {
  const records: unknown[] = [];
  (window as unknown as { __cspViolations: unknown[] }).__cspViolations =
    records;
  document.addEventListener(
    "securitypolicyviolation",
    (event) => {
      const target = event.target instanceof Element ? event.target : null;
      records.push({
        effectiveDirective: event.effectiveDirective,
        blockedURI: event.blockedURI,
        sourceFile: event.sourceFile,
        lineNumber: event.lineNumber,
        columnNumber: event.columnNumber,
        sample: event.sample,
        disposition: event.disposition,
        target:
          target === null
            ? "document"
            : target.outerHTML.slice(0, 140).replace(/\s+/g, " "),
      });
    },
    true, // capture: a handler in the page cannot stop it first
  );
}

/** Starts watching `page`; call before the first `goto`. */
async function observe(page: Page): Promise<Observation> {
  const consoleErrors: ConsoleError[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push({
        text: message.text(),
        url: message.location().url,
      });
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });
  await page.addInitScript(installViolationListener);
  return {
    consoleErrors,
    pageErrors,
    violations: () =>
      page.evaluate(
        () =>
          (window as unknown as { __cspViolations: CspViolation[] })
            .__cspViolations,
      ),
  };
}

function describeViolation(violation: CspViolation): string {
  const where = `${violation.sourceFile}:${violation.lineNumber}:${violation.columnNumber}`;
  const sample =
    violation.sample === ""
      ? ""
      : ` sample=${JSON.stringify(violation.sample)}`;
  return `${violation.effectiveDirective} blocked=${violation.blockedURI}${sample} at ${where} target=${violation.target}`;
}

/**
 * Chrome logs the status of the document itself as a console error when it is
 * not a 2xx. That is the 404 page working, not a fault, and only that exact
 * message about that exact URL is let through.
 */
function isOwnDocumentStatus(
  error: ConsoleError,
  documentUrl: string,
  status: number,
): boolean {
  return (
    status >= 400 &&
    error.url === documentUrl &&
    error.text.startsWith(
      `Failed to load resource: the server responded with a status of ${status}`,
    )
  );
}

/**
 * The browser logs every non-2xx answer as a console error. A page that is
 * meant to show a failed save (`isFailureShown`) has one such line, for the one
 * call its scenario fails on purpose; any other console error still fails.
 */
function isExpectedFailedCall(error: ConsoleError, entry: PageEntry): boolean {
  return (
    entry.isFailureShown === true &&
    error.url.endsWith(`/platform/settings/${FAILING_SETTING_KEY}`) &&
    error.text.startsWith(
      "Failed to load resource: the server responded with a status of 503",
    )
  );
}

// ---------------------------------------------------------- the real pages

/** Shown by the client when a call fails or the body is not what the contract says; the mock never causes them. */
const FAILURE_TEXTS = [
  "The server answered",
  "Could not reach the appmux API",
  "Could not load",
  "Could not refresh",
] as const;

const MIN_MAIN_TEXT_LENGTH = 40;

async function openPage(
  page: Page,
  entry: PageEntry,
): Promise<{ observation: Observation; url: string; status: number }> {
  const observation = await observe(page);
  await applyScenario(page, entry);
  const url = `${server.origin}${entry.path}`;
  const response = await page.goto(url);
  try {
    await waitForPageReady(page, entry);
  } catch (cause) {
    // A refused script is the usual reason a page never draws: say so, rather
    // than only "timeout waiting for the heading".
    const violations = await observation.violations();
    throw new Error(
      [
        `${entry.name} did not finish loading (${url})`,
        `CSP violations: ${JSON.stringify(violations.map(describeViolation), null, 2)}`,
        `console errors: ${JSON.stringify(observation.consoleErrors, null, 2)}`,
        `page errors: ${JSON.stringify(observation.pageErrors, null, 2)}`,
      ].join("\n"),
      { cause },
    );
  }
  return { observation, url, status: response?.status() ?? 0 };
}

interface Overflow {
  clientWidth: number;
  scrollWidth: number;
  offenders: string[];
}

/**
 * How far the page is wider than the window, and which elements stick out: the
 * innermost ones whose box or whose own content passes the window edge and that
 * no scrolling ancestor contains. Runs in the page, so it uses nothing outside itself.
 */
function measureOverflow(): Overflow {
  const root = document.documentElement;
  const overflow: Overflow = {
    clientWidth: root.clientWidth,
    scrollWidth: root.scrollWidth,
    offenders: [],
  };
  if (overflow.scrollWidth <= overflow.clientWidth) return overflow;

  const isClipped = (element: Element): boolean => {
    for (
      let ancestor = element.parentElement;
      ancestor !== null && ancestor !== document.body;
      ancestor = ancestor.parentElement
    ) {
      if (getComputedStyle(ancestor).overflowX !== "visible") return true;
    }
    return false;
  };
  // A block with an unbreakable word keeps its width and overflows by content,
  // so the box edge alone would miss it; its scrollWidth does not.
  const sticksOut = (element: Element): boolean =>
    getComputedStyle(element).overflowX === "visible" &&
    !isClipped(element) &&
    (element.getBoundingClientRect().right > overflow.clientWidth + 1 ||
      element.scrollWidth > element.clientWidth + 1);
  const candidates = [...document.body.querySelectorAll("*")].filter(sticksOut);
  const innermost = candidates.filter(
    (element) =>
      !candidates.some((other) => other !== element && element.contains(other)),
  );
  for (const element of innermost.slice(0, 8)) {
    const id = element.id === "" ? "" : `#${element.id}`;
    const classes = element.getAttribute("class")?.slice(0, 60) ?? "";
    overflow.offenders.push(
      `${element.tagName.toLowerCase()}${id}[${classes}] right=${Math.round(element.getBoundingClientRect().right)} scrollWidth=${element.scrollWidth}`,
    );
  }
  return overflow;
}

/** Below this average a column cannot show a short key or a date without breaking it. */
const MIN_COLUMN_WIDTH_PX = 96;

/**
 * The captions of the tables whose columns average less than `minWidth`. Runs
 * in the page. A stacked table (the phone layout of `DataTable`, drawn as
 * blocks) has no columns to squeeze, so it is left to the next check.
 */
function measureSqueezedTables(minWidth: number): string[] {
  const squeezed: string[] = [];
  for (const table of document.querySelectorAll("table")) {
    if (getComputedStyle(table).display !== "table") continue;
    const columns = table.querySelectorAll("thead th").length;
    const width = table.getBoundingClientRect().width;
    if (columns > 0 && width / columns < minWidth) {
      const caption =
        table.querySelector("caption")?.textContent ?? "(no caption)";
      squeezed.push(`${caption}: ${columns} columns in ${Math.round(width)}px`);
    }
  }
  return squeezed;
}

/**
 * The captions of the stacked tables whose frame scrolls sideways: a stacked
 * table is there so that nothing is clipped, so any sideways scroll in its
 * frame is a defect (a long address that does not wrap, a fixed-width cell).
 * Runs in the page.
 */
function measureScrollingStackedTables(): string[] {
  const scrolling: string[] = [];
  for (const table of document.querySelectorAll("table")) {
    if (getComputedStyle(table).display === "table") continue;
    const frame = table.parentElement;
    if (frame !== null && frame.scrollWidth > frame.clientWidth + 1) {
      const caption =
        table.querySelector("caption")?.textContent ?? "(no caption)";
      scrolling.push(
        `${caption}: content ${frame.scrollWidth}px in a ${frame.clientWidth}px frame`,
      );
    }
  }
  return scrolling;
}

/**
 * The captions of the tables whose visible caption is narrower than the table:
 * a caption inside the phone layout of `DataTable` (a block) shrinks to its own
 * width and wraps in a few letters per line unless it is a block too. A hidden
 * caption (`sr-only`, one pixel wide) is left out. Runs in the page.
 */
function measureNarrowCaptions(): string[] {
  const narrow: string[] = [];
  for (const table of document.querySelectorAll("table")) {
    const caption = table.querySelector("caption");
    if (caption === null) continue;
    const captionWidth = caption.getBoundingClientRect().width;
    const tableWidth = table.getBoundingClientRect().width;
    if (captionWidth > 1 && captionWidth < tableWidth - 1) {
      narrow.push(
        `${caption.textContent}: ${Math.round(captionWidth)}px in a ${Math.round(tableWidth)}px table`,
      );
    }
  }
  return narrow;
}

test.describe("pages under the production CSP", () => {
  for (const entry of [...PAGES, NOT_FOUND_PAGE]) {
    test.describe(entry.name, () => {
      for (const viewport of VIEWPORTS) {
        for (const colorScheme of COLOR_SCHEMES) {
          test.describe(`${viewport.width}px ${colorScheme}`, () => {
            test.use({ viewport, colorScheme });

            test("no CSP violation, console error or empty page", async ({
              page,
            }) => {
              const callsBefore = server.apiCalls.length;
              const { observation, url, status } = await openPage(page, entry);

              const violations = await observation.violations();
              expect(
                violations.map(describeViolation),
                "CSP violations the browser reported",
              ).toEqual([]);

              const consoleErrors = observation.consoleErrors.filter(
                (error) =>
                  !isOwnDocumentStatus(error, url, status) &&
                  !isExpectedFailedCall(error, entry),
              );
              expect(
                consoleErrors.map((error) => `${error.text} (${error.url})`),
                "console errors",
              ).toEqual([]);
              expect(observation.pageErrors, "uncaught page errors").toEqual(
                [],
              );

              expect(status, "status of the document").toBe(entry.status);

              const mainText = await page.locator("main").innerText();
              expect(mainText.trim().length, "text in <main>").toBeGreaterThan(
                MIN_MAIN_TEXT_LENGTH,
              );
              expect(mainText).toContain(entry.dataText);
              if (entry.isFailureShown !== true) {
                for (const failure of FAILURE_TEXTS) {
                  expect(
                    mainText,
                    "the page shows a failed call",
                  ).not.toContain(failure);
                }
              }

              if (entry.apiCall !== null) {
                const answered = server.apiCalls
                  .slice(callsBefore)
                  .filter(
                    (call) =>
                      call.url.startsWith(entry.apiCall ?? "") &&
                      call.status === 200,
                  );
                expect(
                  answered.length,
                  `the page read ${entry.apiCall} from the mock`,
                ).toBeGreaterThan(0);
              }
            });

            test("no horizontal overflow", async ({ page }) => {
              await openPage(page, entry);
              const overflow = await page.evaluate(measureOverflow);
              expect(
                overflow.scrollWidth,
                `page is wider than the ${overflow.clientWidth}px window; sticking out: ${overflow.offenders.join(" | ")}`,
              ).toBeLessThanOrEqual(overflow.clientWidth);
            });

            // The page must not overflow, so a wide table scrolls inside its frame.
            // A table that is squeezed to fit instead splits words mid-letter.
            test("tables keep a readable column width", async ({ page }) => {
              await openPage(page, entry);
              const squeezed = await page.evaluate(
                measureSqueezedTables,
                MIN_COLUMN_WIDTH_PX,
              );
              expect(
                squeezed,
                `tables narrower than ${MIN_COLUMN_WIDTH_PX}px per column`,
              ).toEqual([]);
            });

            test("a visible table caption spans its table", async ({
              page,
            }) => {
              await openPage(page, entry);
              expect(
                await page.evaluate(measureNarrowCaptions),
                "captions narrower than their table",
              ).toEqual([]);
            });

            test("stacked tables are not clipped on a phone", async ({
              page,
            }) => {
              await openPage(page, entry);
              const scrolling = await page.evaluate(
                measureScrollingStackedTables,
              );
              expect(
                scrolling,
                "stacked tables whose frame scrolls sideways",
              ).toEqual([]);
            });
          });
        }
      }
    });
  }
});

// -------------------------------------------------- the header, not vacuous

/** The policy of design 7.3 and FR-021, spelled out on its own (the server builds it from a list). */
const POLICY_WITHOUT_SCRIPTS =
  "default-src 'none'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

/** A second, independent reading of the page's inline scripts (the server has its own). */
function inlineScriptHashes(html: string): string[] {
  const hashes: string[] = [];
  for (const match of html.matchAll(
    /<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g,
  )) {
    const digest = createHash("sha256")
      .update(match[1] ?? "", "utf8")
      .digest("base64");
    hashes.push(`'sha256-${digest}'`);
  }
  return [...new Set(hashes)];
}

test.describe("the server sends the policy the spec names", () => {
  for (const entry of [...PAGES, NOT_FOUND_PAGE]) {
    test(`${entry.name}: header is the reference CSP with a hash per inline script`, async ({
      request,
    }) => {
      const response = await request.get(`${server.origin}${entry.path}`);
      expect(response.status()).toBe(entry.status);
      const hashes = inlineScriptHashes(await response.text());
      expect(
        hashes.length,
        "the page has inline scripts, so a missing hash would matter",
      ).toBeGreaterThan(0);

      const headers = response.headers();
      expect(headers["content-security-policy"]).toBe(
        `${POLICY_WITHOUT_SCRIPTS}; script-src 'self' ${hashes.join(" ")}`,
      );
      expect(headers["content-security-policy"]).not.toContain("unsafe-");
      expect(headers["x-content-type-options"]).toBe("nosniff");
      expect(headers["cross-origin-opener-policy"]).toBe("same-origin");
    });
  }
});

// ------------------------------------------------------ negative controls

test.describe("negative controls: the same harness catches a violation", () => {
  async function openControl(page: Page, name: string) {
    const observation = await observe(page);
    await page.goto(`${server.origin}${CONTROL_PREFIX}/${name}/`);
    return observation;
  }

  test("an inline style attribute and an inline <style> are refused", async ({
    page,
  }) => {
    const observation = await openControl(page, "inline-style");
    await expect
      .poll(async () =>
        (await observation.violations()).map(
          (violation) => violation.effectiveDirective,
        ),
      )
      .toEqual(expect.arrayContaining(["style-src-attr", "style-src-elem"]));

    // Refused for real, not only reported.
    const probe = page.locator("#probe");
    await expect(probe).not.toHaveCSS("background-color", "rgb(255, 0, 0)");
    await expect(probe).not.toHaveCSS("border-top-width", "4px");
    expect(
      observation.consoleErrors.map((error) => error.text).join("\n"),
      "Chrome also logs the refusal to the console",
    ).toContain("Content Security Policy");
  });

  test("an inline script outside the hash list is refused, one in it runs", async ({
    page,
  }) => {
    const observation = await openControl(page, "inline-script");
    await expect
      .poll(async () =>
        (await observation.violations()).map(
          (violation) => violation.effectiveDirective,
        ),
      )
      .toEqual(["script-src-elem"]);

    const ran = await page.evaluate(() => {
      const flags = window as unknown as Record<string, unknown>;
      return {
        covered: flags.__e2eCoveredScriptRan,
        uncovered: flags.__e2eUncoveredScriptRan,
      };
    });
    // The covered script proves the server hashes the way the browser does.
    expect(ran).toEqual({ covered: true, uncovered: undefined });
  });

  test("the real launcher with one script hash missing is refused", async ({
    page,
  }) => {
    const observation = await openControl(page, "missing-hash");
    await expect
      .poll(async () =>
        (await observation.violations()).map(
          (violation) => violation.effectiveDirective,
        ),
      )
      .toContain("script-src-elem");
    const violations = await observation.violations();
    expect(violations.map((violation) => violation.target)).toEqual([
      expect.stringMatching(/^<script/),
    ]);
  });

  test("the overflow check reports a page wider than the window", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openControl(page, "overflow");
    const overflow = await page.evaluate(measureOverflow);
    expect(overflow.scrollWidth).toBeGreaterThan(overflow.clientWidth);
    expect(overflow.offenders.join(" ")).toContain("p#probe");
  });
});
