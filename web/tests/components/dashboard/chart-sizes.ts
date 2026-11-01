import { afterAll, beforeAll } from "vitest";

/**
 * jsdom has no layout, so every element measures 0 x 0 and Recharts'
 * ResponsiveContainer draws nothing. Call this at the top of a test file to
 * give the chart's container a size; the ResizeObserver stub of
 * tests/setup-dom.ts never reports another one, so the chart keeps it. Every
 * other element still measures zero, which is what Recharts' text measuring
 * needs to see to keep all the tick labels.
 *
 * It is installed once per file, not per test, so a test's own
 * `vi.resetAllMocks()` / `vi.restoreAllMocks()` cannot take it away.
 */
export const CHART_WIDTH_PX = 640;
export const CHART_HEIGHT_PX = 240;

const CHART_CLASSES = ["recharts-responsive-container", "recharts-wrapper"];

/** Recharts says this in development on the first render, before it has measured anything; jsdom always starts there. */
const UNMEASURED_WARNING = "of chart should be greater than 0";

export function giveChartsASize(): void {
  let measured: PropertyDescriptor | undefined;
  let warn: typeof console.warn | undefined;

  beforeAll(() => {
    measured = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "getBoundingClientRect",
    );
    Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", {
      configurable: true,
      value(this: HTMLElement): DOMRect {
        const isChart = CHART_CLASSES.some((name) =>
          this.classList.contains(name),
        );
        return isChart
          ? new DOMRect(0, 0, CHART_WIDTH_PX, CHART_HEIGHT_PX)
          : new DOMRect(0, 0, 0, 0);
      },
    });

    warn = console.warn;
    console.warn = (...args: unknown[]) => {
      if (String(args[0]).includes(UNMEASURED_WARNING)) return;
      warn?.(...args);
    };
  });

  afterAll(() => {
    if (measured !== undefined) {
      Object.defineProperty(
        HTMLElement.prototype,
        "getBoundingClientRect",
        measured,
      );
    }
    if (warn !== undefined) console.warn = warn;
  });
}
