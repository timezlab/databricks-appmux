import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import {
  WCAG_AA_NON_TEXT_RATIO,
  WCAG_AA_NORMAL_TEXT_RATIO,
  contrastRatio,
  parseHexColor,
  relativeLuminance,
} from "../support/contrast";

const GLOBALS_CSS_PATH = path.resolve(
  import.meta.dirname,
  "../../src/app/globals.css",
);

describe("parseHexColor", () => {
  it("reads six-digit and three-digit hex, any case", () => {
    assert.deepEqual(parseHexColor("#ffffff"), [255, 255, 255]);
    assert.deepEqual(parseHexColor("#0A58b5"), [10, 88, 181]);
    assert.deepEqual(parseHexColor("#fa0"), [255, 170, 0]);
  });

  it("refuses anything that is not a hex colour, so a token cannot silently skip the check", () => {
    for (const bad of [
      "",
      "white",
      "ffffff",
      "#ffff",
      "#gggggg",
      "rgb(0,0,0)",
      "#ffffff80",
    ]) {
      assert.throws(() => parseHexColor(bad), /hex colour/, bad);
    }
  });
});

describe("contrastRatio (WCAG 2.x)", () => {
  it("is 21 for black on white and 1 for a colour on itself", () => {
    assert.equal(contrastRatio("#000000", "#ffffff"), 21);
    assert.equal(contrastRatio("#336699", "#336699"), 1);
  });

  it("does not depend on which colour is the text", () => {
    assert.equal(
      contrastRatio("#0a58b5", "#ffffff"),
      contrastRatio("#ffffff", "#0a58b5"),
    );
  });

  it("matches the published reference value for #767676 on white (4.54:1, the lightest grey that passes AA)", () => {
    const ratio = contrastRatio("#767676", "#ffffff");
    assert.ok(ratio > 4.5 && ratio < 4.6, `got ${ratio}`);
    assert.ok(contrastRatio("#777777", "#ffffff") < 4.5);
  });

  it("uses the linearised luminance, not the raw channel average", () => {
    // sRGB mid-grey is far darker in linear light than 50 %.
    const luminance = relativeLuminance([128, 128, 128]);
    assert.ok(luminance > 0.2 && luminance < 0.23, `got ${luminance}`);
  });
});

// ---------------------------------------------------------- the real tokens

function declarations(block: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    const [, name, value] = match;
    if (name !== undefined && value !== undefined) {
      found.set(name, value.trim());
    }
  }
  return found;
}

const css = readFileSync(GLOBALS_CSS_PATH, "utf8");
// The header comment names what the file avoids; only the rules are checked.
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");
const lightBlock = /:root\s*\{([^}]*)\}/.exec(rules)?.[1];
const darkBlock =
  /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/.exec(
    rules,
  )?.[1];

assert.ok(lightBlock, "globals.css has a :root block for the light theme");
assert.ok(
  darkBlock,
  "globals.css overrides the colours in @media (prefers-color-scheme: dark)",
);

// `:root` holds colours only: the radius scale and the fonts are theme values.
const lightTokens = declarations(lightBlock);
const darkTokens = declarations(darkBlock);
const lightColourNames = [...lightTokens.keys()];

const THEMES = {
  light: lightTokens,
  dark: new Map([...lightTokens, ...darkTokens]),
};

const TONES = ["neutral", "info", "success", "warning", "danger"] as const;
const CHART_SERIES = [1, 2, 3, 4, 5, 6] as const;

/** Text on its background: WCAG 1.4.3, AA for normal-size text. */
const TEXT_PAIRS: readonly (readonly [text: string, background: string])[] = [
  // Body text, and table rows that take `muted` while the pointer is on them.
  ["--foreground", "--background"],
  ["--foreground", "--card"],
  ["--foreground", "--muted"],
  ["--card-foreground", "--card"],
  ["--popover-foreground", "--popover"],
  ["--secondary-foreground", "--secondary"],
  ["--accent-foreground", "--accent"],
  // Hints, captions, placeholders.
  ["--muted-foreground", "--background"],
  ["--muted-foreground", "--card"],
  ["--muted-foreground", "--muted"],
  // Links, also inside a hovered row and under a hovered ghost button.
  ["--link", "--background"],
  ["--link", "--card"],
  ["--link", "--muted"],
  ["--link", "--accent"],
  // Filled buttons and selected text.
  ["--primary-foreground", "--primary"],
  ["--primary-foreground", "--primary-hover"],
  ["--destructive-foreground", "--destructive"],
  ["--destructive-foreground", "--destructive-hover"],
  // The route strip: the app's route in mono on its paper colour.
  ["--strip-foreground", "--strip"],
  // The error text of a field sits straight on the page or on a card.
  ["--destructive", "--background"],
  ["--destructive", "--card"],
  ...TONES.map((tone) => [`--tone-${tone}-text`, `--tone-${tone}-bg`] as const),
];

/** Boundaries a person needs to find a control, the focus ring, chart marks: WCAG 1.4.11, 3:1. */
const NON_TEXT_PAIRS: readonly (readonly [
  graphic: string,
  background: string,
])[] = [
  // Input, select, textarea borders.
  ["--input", "--background"],
  ["--input", "--card"],
  ["--ring", "--background"],
  ["--ring", "--card"],
  // The ring around a sidebar item under the pointer.
  ["--ring", "--muted"],
  // Filled buttons, and the bar of the selected navigation item.
  ["--primary", "--background"],
  ["--primary", "--card"],
  ["--destructive", "--background"],
  // Lamps, badge outlines, the bar of an alert, and the filled cells of a capacity strip on the page and on a card.
  ...TONES.flatMap((tone) => [
    [`--tone-${tone}-border`, "--background"] as const,
    [`--tone-${tone}-border`, "--card"] as const,
  ]),
  // Chart series on the page and on a card.
  ...CHART_SERIES.flatMap((series) => [
    [`--chart-${series}`, "--background"] as const,
    [`--chart-${series}`, "--card"] as const,
  ]),
];

function colour(theme: Map<string, string>, token: string): string {
  const value = theme.get(token);
  assert.ok(value, `${token} is defined`);
  return value;
}

for (const [themeName, theme] of Object.entries(THEMES)) {
  describe(`theme tokens, ${themeName}`, () => {
    for (const [text, background] of TEXT_PAIRS) {
      it(`${text} on ${background} is at least ${WCAG_AA_NORMAL_TEXT_RATIO}:1`, () => {
        const ratio = contrastRatio(
          colour(theme, text),
          colour(theme, background),
        );
        assert.ok(
          ratio >= WCAG_AA_NORMAL_TEXT_RATIO,
          `${text} ${colour(theme, text)} on ${background} ${colour(theme, background)} is ${ratio.toFixed(2)}:1`,
        );
      });
    }

    for (const [graphic, background] of NON_TEXT_PAIRS) {
      it(`${graphic} against ${background} is at least ${WCAG_AA_NON_TEXT_RATIO}:1`, () => {
        const ratio = contrastRatio(
          colour(theme, graphic),
          colour(theme, background),
        );
        assert.ok(
          ratio >= WCAG_AA_NON_TEXT_RATIO,
          `${graphic} ${colour(theme, graphic)} against ${background} ${colour(theme, background)} is ${ratio.toFixed(2)}:1`,
        );
      });
    }
  });
}

describe("globals.css consistency", () => {
  it("every colour token is a plain hex value, so the calculation above covers it", () => {
    for (const name of lightColourNames) {
      assert.doesNotThrow(() => parseHexColor(colour(lightTokens, name)), name);
    }
    for (const [name, value] of darkTokens) {
      assert.doesNotThrow(() => parseHexColor(value), name);
    }
  });

  it("the dark theme overrides exactly the colour tokens of the light theme", () => {
    assert.deepEqual(
      [...darkTokens.keys()].toSorted(),
      lightColourNames.toSorted(),
    );
  });

  it("every colour token has a Tailwind colour, and the palette is closed to everything else", () => {
    assert.match(rules, /--color-\*\s*:\s*initial\s*;/);
    for (const name of lightColourNames) {
      const token = name.slice(2);
      assert.match(
        rules,
        new RegExp(`--color-${token}\\s*:\\s*var\\(${name}\\)\\s*;`),
        `${name} is exposed as the --color-${token} colour`,
      );
    }
  });

  it("every var(--token) the stylesheet reads is defined", () => {
    const declared = new Set(
      [...rules.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]),
    );
    const used = [...rules.matchAll(/var\(\s*(--[\w-]+)/g)].map(
      (match) => match[1],
    );
    assert.ok(used.length > 0, "the stylesheet reads its tokens");
    for (const name of used) {
      assert.ok(declared.has(name), `${name} is defined`);
    }
  });

  it("imports only Tailwind, and loads nothing from outside the page (strict CSP: style-src, img-src and font-src are 'self')", () => {
    const imports = [...rules.matchAll(/@import\s+["']([^"']+)["']/g)].map(
      (match) => match[1],
    );
    assert.deepEqual(imports, ["tailwindcss"]);
    assert.doesNotMatch(rules, /url\(/i);
    assert.doesNotMatch(rules, /https?:\/\//i);
    assert.doesNotMatch(rules, /@font-face/i);
  });
});
