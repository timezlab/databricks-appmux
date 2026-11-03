/**
 * Motion for both values of `prefers-reduced-motion`
 * (docs/product/admin-ui-design.md, Motion): while a page waits for its data,
 * the spinner and the skeleton each run their animation, and asking for less
 * motion swaps it for a slower, calmer one; it never stops it. The launcher's
 * call for the app list is held open so the loading state stays on screen
 * while the browser is asked what it is animating.
 *
 * `npm run build` first; `npm run test:e2e` runs this.
 */
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { API_PREFIX, BASE_PATH, startServer } from "./server.ts";
import type { E2eServer } from "./server.ts";
import { READY_TIMEOUT_MS } from "./pages.ts";

let server: E2eServer;

test.beforeAll(async () => {
  server = await startServer();
});
test.afterAll(async () => {
  await server.close();
});

interface RunningAnimation {
  name: string;
  /** Milliseconds; a CSS animation's duration is always a number. Anything else is reported as -1 and fails. */
  durationMs: number;
  iterations: number;
  playState: string;
}

/** What each part animates, per value of `reducedMotion` (globals.css, the `animate-*` utilities). */
const EXPECTED = {
  "no-preference": {
    spinner: { name: "spinner-turn", durationMs: 900 },
    skeleton: { name: "skeleton-pulse", durationMs: 1400 },
  },
  reduce: {
    spinner: { name: "spinner-pulse", durationMs: 1600 },
    skeleton: { name: "skeleton-pulse", durationMs: 2200 },
  },
} as const;

const PARTS = {
  spinner: ".animate-spinner",
  skeleton: ".animate-skeleton",
} as const;

/** Opens the launcher with its app list call never answered, so it stays in its loading state. */
async function openLauncherStillLoading(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === `${API_PREFIX}/apps`,
    () => {
      // Never fulfilled: the page waits for its data for as long as the test looks.
    },
  );
  await page.goto(`${server.origin}${BASE_PATH}/`);
  await page
    .getByRole("status")
    .filter({ hasText: "Loading apps" })
    .waitFor({ timeout: READY_TIMEOUT_MS });
}

/** The CSS animations running on the elements `selector` matches. */
async function animationsOf(
  page: Page,
  selector: string,
): Promise<RunningAnimation[]> {
  return page.evaluate((target) => {
    return [...document.querySelectorAll(target)].flatMap((element) =>
      element.getAnimations().map((animation) => {
        const timing = animation.effect?.getComputedTiming();
        const duration = timing?.duration;
        return {
          name:
            animation instanceof CSSAnimation ? animation.animationName : "",
          durationMs: typeof duration === "number" ? duration : -1,
          iterations: timing?.iterations ?? 0,
          playState: animation.playState,
        };
      }),
    );
  }, selector);
}

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test.describe(`prefers-reduced-motion: ${reducedMotion}`, () => {
    test.use({ reducedMotion });

    test("the spinner and the skeleton keep moving, at the speed the table sets", async ({
      page,
    }) => {
      await openLauncherStillLoading(page);

      for (const [part, selector] of Object.entries(PARTS)) {
        const expected = EXPECTED[reducedMotion][part as keyof typeof PARTS];
        const animations = await animationsOf(page, selector);
        expect(
          animations.length,
          `${part} elements on the page`,
        ).toBeGreaterThan(0);
        for (const animation of animations) {
          expect(animation, part).toEqual({
            name: expected.name,
            durationMs: expected.durationMs,
            iterations: Infinity,
            playState: "running",
          });
        }
      }

      await page.unrouteAll({ behavior: "ignoreErrors" });
    });
  });
}
