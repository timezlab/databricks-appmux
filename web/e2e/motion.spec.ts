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

import {
  API_PREFIX,
  BASE_PATH,
  PRIMITIVES_PREFIX,
  startServer,
} from "./server.ts";
import type { E2eServer } from "./server.ts";
import { READY_TIMEOUT_MS } from "./pages.ts";
import { READY_ATTRIBUTE } from "./primitives/ready.ts";

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

/** What a menu or popover plays when it opens (globals.css, `animate-menu-in`): a fade and a 4 px slide, or the fade alone. */
const OPENING = {
  "no-preference": { name: "menu-in", durationMs: 140 },
  reduce: { name: "menu-fade-in", durationMs: 120 },
} as const;

/**
 * The primitives whose floating content opens with `animate-menu-in`: the trigger to press, how
 * its library opens (Radix's menu opens on `pointerdown`, its popover on `click`) and the
 * content's slot.
 */
const FLOATING = [
  {
    part: "popover",
    trigger: "Open popover",
    opensOn: "click",
    slot: "popover-content",
  },
  {
    part: "dropdown menu",
    trigger: "Actions",
    opensOn: "pointerdown",
    slot: "dropdown-menu-content",
  },
] as const;

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

    for (const floating of FLOATING) {
      test(`a ${floating.part} opens with the animation the table sets`, async ({
        page,
      }) => {
        await page.goto(`${server.origin}${PRIMITIVES_PREFIX}/`);
        await expect(page.locator(`html[${READY_ATTRIBUTE}]`)).toBeAttached();

        // Press the trigger and look on the next frame: the animation lasts
        // 120 to 140 ms, too short to race a separate round trip to the page.
        const animations = await page
          .getByRole("button", { name: floating.trigger })
          .evaluate(
            async (trigger, { slot, opensOn }) => {
              if (!(trigger instanceof HTMLElement)) {
                throw new Error("the trigger is not an HTML element");
              }
              if (opensOn === "click") {
                trigger.click();
              } else {
                trigger.dispatchEvent(
                  new PointerEvent("pointerdown", {
                    bubbles: true,
                    button: 0,
                    pointerType: "mouse",
                  }),
                );
              }
              await new Promise((resolve) => requestAnimationFrame(resolve));
              const content = document.querySelector(`[data-slot="${slot}"]`);
              return (content?.getAnimations() ?? []).map((animation) => {
                const duration = animation.effect?.getComputedTiming().duration;
                return {
                  name:
                    animation instanceof CSSAnimation
                      ? animation.animationName
                      : "",
                  durationMs: typeof duration === "number" ? duration : -1,
                };
              });
            },
            { slot: floating.slot, opensOn: floating.opensOn },
          );

        expect(animations, `${floating.part} animations`).toEqual([
          OPENING[reducedMotion],
        ]);
      });
    }
  });
}
