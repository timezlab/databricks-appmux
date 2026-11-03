/**
 * The eight app tones of the breakdown must stay tellable apart without
 * relying on hue alone: a person who cannot tell two hues apart still tells a
 * plain segment from a striped one from a dotted one. So each app has its own
 * pair of colour and texture, and only the first app is plain: two plain
 * tones would differ by hue alone. Each colour is a `--chart-N` token, which
 * tests/contract/contrast.test.ts judges against the page and the card in
 * both themes.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { ChartTone } from "@/lib/dashboard/model";
import { seriesStyle } from "@/lib/dashboard/series-style";

const APP_TONES: readonly ChartTone[] = [
  "series-1",
  "series-2",
  "series-3",
  "series-4",
  "series-5",
  "series-6",
  "series-7",
  "series-8",
];

/** The tokens contrast.test.ts judges (its CHART_SERIES). */
const JUDGED_CHART_TOKEN = /^var\(--chart-[1-8]\)$/;

describe("the eight app tones of the breakdown", () => {
  it("give each app its own pair of colour and texture", () => {
    const pairs = APP_TONES.map((tone) => {
      const { color, texture } = seriesStyle(tone);
      return `${color} ${texture}`;
    });
    assert.equal(new Set(pairs).size, APP_TONES.length, pairs.join(" | "));
  });

  it("leave at most the first app plain, so no two plain tones differ by hue alone", () => {
    const plain = APP_TONES.filter(
      (tone) => seriesStyle(tone).texture === "none",
    );
    assert.ok(
      plain.length === 0 || (plain.length === 1 && plain[0] === "series-1"),
      `plain: ${plain.join(", ")}`,
    );
  });

  it("paint with a series token that the contrast test judges, each app its own", () => {
    const colors = APP_TONES.map((tone) => seriesStyle(tone).color);
    for (const color of colors) {
      assert.match(color, JUDGED_CHART_TOKEN);
    }
    assert.equal(new Set(colors).size, APP_TONES.length, colors.join(", "));
  });
});
