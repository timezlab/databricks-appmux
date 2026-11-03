/**
 * What theme.spec.ts and shell.spec.ts watch while they drive a page: every
 * CSP violation, console error and page error. A smaller cousin of the
 * observer in csp.spec.ts, which also records where a violation came from.
 */
import type { Page } from "@playwright/test";

export interface PageWatch {
  /** `<directive> <blocked URI>` for each violation the page reported. */
  cspViolations: () => Promise<string[]>;
  /** `<text> (<url>)` for each console error. */
  consoleErrors: string[];
  pageErrors: string[];
}

/** Runs in the page before any of its scripts; Playwright sends its source, so it uses nothing from outside. */
function recordCspViolations(): void {
  const records: string[] = [];
  (window as unknown as { __cspViolations: string[] }).__cspViolations =
    records;
  document.addEventListener(
    "securitypolicyviolation",
    (event) => {
      records.push(`${event.effectiveDirective} ${event.blockedURI}`);
    },
    true,
  );
}

/** Starts watching `page`; call before the first `goto`. */
export async function watchPage(page: Page): Promise<PageWatch> {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(`${message.text()} (${message.location().url})`);
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });
  await page.addInitScript(recordCspViolations);
  return {
    consoleErrors,
    pageErrors,
    cspViolations: () =>
      page.evaluate(
        () =>
          (window as unknown as { __cspViolations: string[] }).__cspViolations,
      ),
  };
}

/** Everything `watch` saw, in one object, so a failing test shows it all at once. */
export async function problemsOf(watch: PageWatch): Promise<{
  cspViolations: string[];
  consoleErrors: string[];
  pageErrors: string[];
}> {
  return {
    cspViolations: await watch.cspViolations(),
    consoleErrors: watch.consoleErrors,
    pageErrors: watch.pageErrors,
  };
}

export const NO_PROBLEMS = {
  cspViolations: [],
  consoleErrors: [],
  pageErrors: [],
} as const;

/** Whether the page is wider than its window (a sideways scroll on a phone). */
export function hasHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(
    () =>
      document.documentElement.scrollWidth >
      document.documentElement.clientWidth,
  );
}
