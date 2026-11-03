/**
 * The tones of the breakdown (eight apps and the grey of every app below the
 * cut) must stay tellable apart by a reader who cannot tell some hues apart:
 * a reader with deuteranopia, protanopia or tritanopia. So each tone is a pair
 * of colour and texture, and the two do the work together:
 *
 * - No texture serves more than two tones, and only the first app is plain:
 *   two plain tones would differ by colour alone.
 * - The two tones that share a texture differ in colour by at least
 *   CIEDE2000 10 for normal vision and for each of the three dichromacies
 *   (Machado 2009, severity 1.0), in the light and the dark theme. The
 *   values are the real `--chart-N` and `--muted-foreground` hex values of
 *   globals.css, which tests/contract/contrast.test.ts judges against the page
 *   and the card in both themes.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import type { ChartTone, ToneTexture } from "@/lib/dashboard/model";
import { seriesStyle } from "@/lib/dashboard/series-style";
import {
  VISION_CONDITIONS,
  ciede2000,
  labAsSeen,
} from "../support/color-difference";
import type { VisionCondition } from "../support/color-difference";
import { parseHexColor } from "../support/contrast";

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
const BREAKDOWN_TONES: readonly ChartTone[] = [...APP_TONES, "other"];

/** The tokens contrast.test.ts judges (its CHART_SERIES). */
const JUDGED_CHART_TOKEN = /^var\(--chart-[1-8]\)$/;
/** A colour the SVG reads from a token: `var(--chart-2)`. */
const TOKEN_REFERENCE = /^var\((--[\w-]+)\)$/;

const MAX_TONES_PER_TEXTURE = 2;
/** CIEDE2000: about 1 is just noticeable; 10 reads as another colour at a glance. */
const MIN_SAME_TEXTURE_DIFFERENCE = 10;

// --------------------------------------------------------- the real tokens

const GLOBALS_CSS_PATH = path.resolve(
  import.meta.dirname,
  "../../src/app/globals.css",
);
const rules = readFileSync(GLOBALS_CSS_PATH, "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function declarations(block: string | undefined): Map<string, string> {
  assert.ok(block, "globals.css has the theme block");
  const found = new Map<string, string>();
  for (const [, name, value] of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    if (name !== undefined && value !== undefined)
      found.set(name, value.trim());
  }
  return found;
}

const lightTokens = declarations(/:root\s*\{([^}]*)\}/.exec(rules)?.[1]);
// Dark is written twice (the Dark choice, and System on a dark OS);
// contrast.test.ts holds the two blocks equal, so the first stands for both.
const darkTokens = new Map([
  ...lightTokens,
  ...declarations(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/.exec(rules)?.[1]),
]);
const THEMES = { light: lightTokens, dark: darkTokens };

function hexOf(theme: Map<string, string>, tone: ChartTone): string {
  const { color } = seriesStyle(tone);
  const token = TOKEN_REFERENCE.exec(color)?.[1];
  assert.ok(token, `${tone} paints with a token: ${color}`);
  const hex = theme.get(token);
  assert.ok(hex, `${token} is defined`);
  return hex;
}

/** Every two tones of the breakdown drawn with the same texture. */
function sameTexturePairs(): [ChartTone, ChartTone, ToneTexture][] {
  const pairs: [ChartTone, ChartTone, ToneTexture][] = [];
  BREAKDOWN_TONES.forEach((first, index) => {
    for (const second of BREAKDOWN_TONES.slice(index + 1)) {
      const { texture } = seriesStyle(first);
      if (texture === seriesStyle(second).texture) {
        pairs.push([first, second, texture]);
      }
    }
  });
  return pairs;
}

describe("the colour maths of this test", () => {
  // Sharma, Wu and Dalal (2005), table 1: L*a*b* pairs and their CIEDE2000.
  it.each([
    [[50, 2.6772, -79.7751], [50, 0, -82.7485], 2.0425],
    [[50, 3.1571, -77.2803], [50, 0, -82.7485], 2.8615],
    [[50, 0, 0], [50, -1, 2], 2.3669],
    [[50, 2.5, 0], [73, 25, -18], 27.1492],
    [[50, 2.5, 0], [61, -5, 29], 22.8977],
    [[50, 2.5, 0], [56, -27, -3], 31.903],
    [[50, 2.5, 0], [58, 24, 15], 19.4535],
    [[50, 2.49, -0.001], [50, -2.49, 0.0011], 7.2195],
    [[60.2574, -34.0099, 36.2677], [60.4626, -34.1751, 39.4387], 1.2644],
    [[2.0776, 0.0795, -1.135], [0.9033, -0.0636, -0.5514], 0.9082],
  ] as const)("CIEDE2000 of %j and %j is %f", (first, second, expected) => {
    assert.equal(Number(ciede2000(first, second).toFixed(4)), expected);
    assert.equal(Number(ciede2000(second, first).toFixed(4)), expected);
  });

  it("reads sRGB red and white as their published CIELAB values", () => {
    const [lightness, a, b] = labAsSeen([255, 0, 0], "normal");
    assert.ok(Math.abs(lightness - 53.2408) < 0.01, `L* ${lightness}`);
    assert.ok(Math.abs(a - 80.0925) < 0.01, `a* ${a}`);
    assert.ok(Math.abs(b - 67.2032) < 0.01, `b* ${b}`);
    const white = labAsSeen([255, 255, 255], "normal");
    assert.ok(Math.abs(white[0] - 100) < 0.01 && Math.abs(white[1]) < 0.01);
  });

  it("keeps a grey grey under every simulation", () => {
    for (const condition of VISION_CONDITIONS) {
      const grey = labAsSeen([119, 119, 119], condition);
      const normal = labAsSeen([119, 119, 119], "normal");
      assert.ok(ciede2000(grey, normal) < 0.01, condition);
    }
  });

  it("finds the confusions the review measured with its own script", () => {
    // review2-web/cvd.py: #c2410c and #4d7c0f look alike to a deuteranope.
    const difference = (
      first: string,
      second: string,
      condition: VisionCondition,
    ) =>
      ciede2000(
        labAsSeen(parseHexColor(first), condition),
        labAsSeen(parseHexColor(second), condition),
      ).toFixed(1);
    assert.equal(difference("#c2410c", "#4d7c0f", "deuteranopia"), "5.1");
    assert.equal(difference("#ff9a6c", "#a8d45a", "deuteranopia"), "3.4");
    assert.equal(difference("#c2410c", "#be185d", "tritanopia"), "2.7");
    assert.equal(difference("#15803d", "#0e7490", "tritanopia"), "5.7");
  });
});

describe("the tones of the breakdown", () => {
  it("give each tone its own pair of colour and texture", () => {
    const pairs = BREAKDOWN_TONES.map((tone) => {
      const { color, texture } = seriesStyle(tone);
      return `${color} ${texture}`;
    });
    assert.equal(
      new Set(pairs).size,
      BREAKDOWN_TONES.length,
      pairs.join(" | "),
    );
  });

  it("leave at most the first app plain, so no two plain tones differ by colour alone", () => {
    const plain = BREAKDOWN_TONES.filter(
      (tone) => seriesStyle(tone).texture === "none",
    );
    assert.ok(
      plain.length === 0 || (plain.length === 1 && plain[0] === "series-1"),
      `plain: ${plain.join(", ")}`,
    );
  });

  it(`use no texture for more than ${MAX_TONES_PER_TEXTURE} tones`, () => {
    const tonesByTexture = new Map<ToneTexture, ChartTone[]>();
    for (const tone of BREAKDOWN_TONES) {
      const { texture } = seriesStyle(tone);
      tonesByTexture.set(texture, [
        ...(tonesByTexture.get(texture) ?? []),
        tone,
      ]);
    }
    const crowded = [...tonesByTexture]
      .filter(([, tones]) => tones.length > MAX_TONES_PER_TEXTURE)
      .map(([texture, tones]) => `${texture}: ${tones.join(", ")}`);
    assert.deepEqual(crowded, []);
  });

  it("paint each app with a series token that the contrast test judges, each app its own", () => {
    const colors = APP_TONES.map((tone) => seriesStyle(tone).color);
    for (const color of colors) {
      assert.match(color, JUDGED_CHART_TOKEN);
    }
    assert.equal(new Set(colors).size, APP_TONES.length, colors.join(", "));
  });

  for (const [themeName, theme] of Object.entries(THEMES)) {
    for (const condition of VISION_CONDITIONS) {
      it(`keep two tones of one texture at least CIEDE2000 ${MIN_SAME_TEXTURE_DIFFERENCE} apart: ${themeName} theme, ${condition} vision`, () => {
        const tooClose = sameTexturePairs()
          .map(([first, second, texture]) => {
            const difference = ciede2000(
              labAsSeen(parseHexColor(hexOf(theme, first)), condition),
              labAsSeen(parseHexColor(hexOf(theme, second)), condition),
            );
            return { first, second, texture, difference };
          })
          .filter(({ difference }) => difference < MIN_SAME_TEXTURE_DIFFERENCE)
          .map(
            ({ first, second, texture, difference }) =>
              `${first} ${hexOf(theme, first)} and ${second} ${hexOf(theme, second)} (${texture}): ${difference.toFixed(1)}`,
          );
        assert.deepEqual(tooClose, []);
      });
    }
  }
});
