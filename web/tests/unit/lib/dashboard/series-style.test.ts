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
