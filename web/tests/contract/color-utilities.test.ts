/**
 * Static guard for how colour reaches the markup. The contrast test
 * (contrast.test.ts) calculates every token pair of globals.css, but it can
 * only see a colour that is a token. So a class may not paint one another way:
 *  - no opacity modifier on a token colour (`bg-primary/90`): the colour on
 *    screen is a blend the test never computed;
 *  - no colour literal in an arbitrary value (`bg-[#fafafa]`, `text-[oklch(...)]`);
 *  - no colour of Tailwind's default palette (`bg-red-500`, `text-white`): the
 *    palette is closed in globals.css, so the class would emit nothing and
 *    the element would silently keep another colour.
 * System colour keywords (`bg-[Canvas]`, `border-[color:Highlight]`) are
 * allowed: they exist for forced-colors mode and follow the user's choice.
 *
 * Tailwind reads class names out of every file under src/ (`source("..")` in
 * globals.css), comments and test fixtures included, so this scan reads the
 * raw text of every file there rather than the syntax tree: an example in a
 * comment would still be emitted as CSS.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

const SRC_DIR = path.resolve(import.meta.dirname, "../../src");
const GLOBALS_CSS_PATH = path.join(SRC_DIR, "app/globals.css");

const globalsCss = readFileSync(GLOBALS_CSS_PATH, "utf8");
const rules = globalsCss.replace(/\/\*[\s\S]*?\*\//g, "");

/** `--card-foreground` and the rest of the `:root` block, as the colour names a class can use. */
const TOKEN_NAMES = [
  ...(/:root\s*\{([^}]*)\}/.exec(rules)?.[1] ?? "").matchAll(/--([\w-]+)\s*:/g),
]
  .map((match) => match[1] ?? "")
  .filter((name) => name !== "radius")
  // Longest first, so `card-foreground` is tried before `card`.
  .toSorted((a, b) => b.length - a.length);

const UTILITIES =
  "bg|text|border|border-[xytblrse]|ring|outline|fill|stroke|decoration|divide|accent|caret|shadow|from|via|to|placeholder|selection";
const DEFAULT_PALETTE =
  "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose";

const OPACITY_MODIFIER = new RegExp(
  `(?<![\\w-])(?:${UTILITIES})-(?:${TOKEN_NAMES.join("|")})/(?:\\d|\\[|\\()[^\\s"'\\x60]*`,
  "g",
);
const COLOUR_LITERAL =
  /\[(?:[a-z-]+:)?(?:#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix|color)\()[^\]\s"'`]*\]?/g;
const DEFAULT_PALETTE_COLOUR = new RegExp(
  `(?<![\\w-])(?:${UTILITIES})-(?:(?:${DEFAULT_PALETTE})-\\d{2,3}|white|black)(?![\\w-])`,
  "g",
);

const RULES: readonly (readonly [reason: string, pattern: RegExp])[] = [
  ["an opacity modifier on a token colour", OPACITY_MODIFIER],
  ["a colour literal in an arbitrary value", COLOUR_LITERAL],
  ["a colour of Tailwind's default palette", DEFAULT_PALETTE_COLOUR],
];

/** The offending class fragments in `text`, with the rule each breaks. */
function colourViolations(text: string): string[] {
  return RULES.flatMap(([reason, pattern]) =>
    [...text.matchAll(pattern)].map((match) => `${match[0]} (${reason})`),
  );
}

function filesUnder(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(entryPath) : [entryPath];
  });
}

describe("the guard itself (each rule is caught, nothing else is)", () => {
  it("catches an opacity modifier on a token colour, however it is written", () => {
    for (const classes of [
      "bg-primary/90",
      "hover:bg-primary-hover/80",
      "border-input/50",
      "text-tone-info-text/[0.5]",
      "bg-card-foreground/(--alpha)",
    ]) {
      assert.equal(colourViolations(`"${classes}"`).length, 1, classes);
    }
  });

  it("catches a colour literal in an arbitrary value", () => {
    for (const classes of [
      "bg-[#fafafa]",
      "text-[#fff]",
      "border-[rgb(1,2,3)]",
      "bg-[oklch(0.5_0.1_200)]",
      "text-[color:#123456]",
    ]) {
      assert.equal(colourViolations(`"${classes}"`).length, 1, classes);
    }
  });

  it("catches a colour of the default palette", () => {
    for (const classes of [
      "bg-red-500",
      "hover:text-blue-600",
      "border-slate-200",
      "text-white",
      "bg-black",
    ]) {
      assert.equal(colourViolations(`"${classes}"`).length, 1, classes);
    }
  });

  it("lets token colours, system colours and a slash that is not an opacity through", () => {
    for (const classes of [
      "bg-primary text-primary-foreground hover:bg-primary-hover",
      "border-tone-danger-border bg-tone-danger-bg text-tone-danger-text",
      "bg-[Canvas] text-[CanvasText] border-b-[color:Highlight]",
      "bg-transparent text-current border-input",
      "w-1/2 basis-1/3 size-4 text-sm text-xs bg-(--bar)",
      "[--bar:var(--tone-success-border)]",
      "https://example.invalid/bg-primary/",
    ]) {
      assert.deepEqual(colourViolations(`"${classes}"`), [], classes);
    }
  });

  it("knows the real tokens, including the long names", () => {
    for (const name of [
      "background",
      "card-foreground",
      "primary-hover",
      "tone-warning-border",
      "chart-6",
    ]) {
      assert.ok(TOKEN_NAMES.includes(name), name);
    }
    assert.ok(!TOKEN_NAMES.includes("radius"));
  });
});

describe("the sources follow the rule", () => {
  it("no file under src/ paints a colour the contrast test cannot see", () => {
    const files = filesUnder(SRC_DIR);
    assert.ok(files.length > 50, "the scan finds the sources");
    const found = files.flatMap((file) =>
      colourViolations(readFileSync(file, "utf8")).map(
        (violation) => `${path.relative(SRC_DIR, file)}: ${violation}`,
      ),
    );
    assert.deepEqual(found, []);
  });

  it("Tailwind is told to read src/ only, so nothing outside it (tests, docs) can emit CSS", () => {
    assert.match(rules, /@import\s+"tailwindcss"\s+source\("\.\."\)/);
  });
});
