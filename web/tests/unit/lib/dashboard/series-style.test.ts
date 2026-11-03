import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import type { ChartTone } from "@/lib/dashboard/model";
import { seriesStyle } from "@/lib/dashboard/series-style";

const TONES: readonly ChartTone[] = ["primary", "warning", "danger"];

const GLOBALS_CSS = readFileSync(
  path.resolve(import.meta.dirname, "../../../../src/app/globals.css"),
  "utf8",
);

/**
 * The lines that declare `--chart-N` in globals.css: the light `:root`, the
 * Dark choice and System on a dark OS (ADR 0019; the contrast test checks that
 * the two dark blocks hold the same values).
 */
function chartTokenDeclarations(variable: string): string[] {
  return GLOBALS_CSS.split("\n").filter((line) =>
    line.trim().startsWith(`${variable}:`),
  );
}

describe("seriesStyle", () => {
  it("never tells two tones apart by colour alone: texture, dash and marker all differ", () => {
    for (const field of ["color", "texture", "dash", "marker"] as const) {
      const values = TONES.map((tone) => seriesStyle(tone)[field]);
      assert.equal(
        new Set(values).size,
        TONES.length,
        `${field}: ${values.join(", ")}`,
      );
    }
  });

  it("paints with a series token of globals.css, which is declared for light and for dark", () => {
    for (const tone of TONES) {
      const match = /^var\((--chart-[1-6])\)$/.exec(seriesStyle(tone).color);
      assert.ok(
        match?.[1] !== undefined,
        `${tone}: ${seriesStyle(tone).color}`,
      );
      assert.equal(
        chartTokenDeclarations(match[1]).length,
        3,
        `${match[1]} must be declared in :root and in both dark blocks`,
      );
    }
  });

  it("keeps the plain tone solid, with no texture and no dash, so the main series is the simplest to read", () => {
    const primary = seriesStyle("primary");
    assert.equal(primary.texture, "none");
    assert.equal(primary.dash, undefined);
  });
});

describe("the app tones of the breakdown", () => {
  // In stacking order: "other" sits on top of the eighth app.
  const APP_TONES: readonly ChartTone[] = [
    "series-1",
    "series-2",
    "series-3",
    "series-4",
    "series-5",
    "series-6",
    "series-7",
    "series-8",
    "other",
  ];

  it("give each app its own pair of colour and texture, so no two apps look alike", () => {
    const looks = APP_TONES.map((tone) => {
      const style = seriesStyle(tone);
      return `${style.color} ${style.texture}`;
    });
    assert.equal(new Set(looks).size, APP_TONES.length, looks.join(" | "));
  });

  it("make two neighbours in a stack differ in texture, since some neighbouring tokens fail the colour-blind check", () => {
    for (let index = 1; index < APP_TONES.length; index += 1) {
      const below = APP_TONES[index - 1];
      const above = APP_TONES[index];
      assert.ok(below !== undefined && above !== undefined);
      assert.notEqual(
        seriesStyle(below).texture,
        seriesStyle(above).texture,
        `${below} and ${above}`,
      );
    }
  });

  it("paint the apps below the cut in a neutral token, never a ninth hue", () => {
    assert.equal(seriesStyle("other").color, "var(--muted-foreground)");
  });
});
