/**
 * Every animation keeps moving under `prefers-reduced-motion: reduce`
 * (docs/product/admin-ui-design.md, Motion). The old blanket rule set every
 * animation to 0.01 ms, which stopped the spinner, so a person who asked for
 * less motion saw a page that looked frozen. This reads globals.css and fails
 * when a reduce block switches an animation off, or when one of the
 * utilities that must keep moving has no reduced animation of its own.
 * e2e/motion.spec.ts checks the same in Chromium, by name and duration.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

const GLOBALS_CSS_PATH = path.resolve(
  import.meta.dirname,
  "../../src/app/globals.css",
);

/** The utilities that say "something is happening"; none may stand still. */
const MUST_KEEP_MOVING = [
  "animate-spinner",
  "animate-lamp-breathe",
  "animate-skeleton",
  "animate-menu-in",
  "animate-menu-out",
] as const;

const REDUCE_QUERY =
  /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{/g;

/** Shorter than this, an animation or transition is not seen: it is switched off. */
const SHORTEST_VISIBLE_MS = 50;

/** What switches motion off instead of changing it. */
const STOPPERS: readonly (readonly [reason: string, pattern: RegExp])[] = [
  ["animation: none", /animation(?:-name)?\s*:\s*none/i],
  ["a single iteration", /animation-iteration-count\s*:\s*1\b/i],
  ["transition: none", /transition(?:-property)?\s*:\s*none/i],
];

/** Every time value of the `animation*` or `transition*` declarations in `block`, in ms. */
function durationsMs(
  block: string,
  property: "animation" | "transition",
): number[] {
  const declarations = block.matchAll(
    new RegExp(`${property}(?:-duration)?\\s*:([^;}]*)`, "gi"),
  );
  return [...declarations].flatMap((declaration) =>
    [...(declaration[1] ?? "").matchAll(/(\d*\.?\d+)(ms|s)\b/g)].map(
      ([, amount, unit]) => Number(amount) * (unit === "s" ? 1000 : 1),
    ),
  );
}

/** The text between the `{` at `open` and its matching `}`. */
function blockBody(css: string, open: number): string {
  let depth = 0;
  for (let index = open; index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") depth -= 1;
    if (depth === 0) return css.slice(open + 1, index);
  }
  throw new Error(`unbalanced braces after offset ${String(open)}`);
}

function reduceBlocks(css: string): string[] {
  return [...css.matchAll(REDUCE_QUERY)].map((match) =>
    blockBody(css, match.index + match[0].length - 1),
  );
}

function utilityBody(css: string, name: string): string | null {
  const match = new RegExp(`@utility\\s+${name}\\s*\\{`).exec(css);
  return match ? blockBody(css, match.index + match[0].length - 1) : null;
}

function stoppers(block: string): string[] {
  const found = STOPPERS.flatMap(([reason, pattern]) =>
    pattern.test(block) ? [reason] : [],
  );
  for (const [property, article] of [
    ["animation", "an"],
    ["transition", "a"],
  ] as const) {
    if (durationsMs(block, property).some((ms) => ms < SHORTEST_VISIBLE_MS)) {
      found.push(
        `${article} ${property} shorter than ${String(SHORTEST_VISIBLE_MS)} ms`,
      );
    }
  }
  return found;
}

/** The keyframe names an `animation:` shorthand in `block` plays. */
function animationNames(block: string): string[] {
  return [...block.matchAll(/animation\s*:\s*([\w-]+)/g)].map(
    (match) => match[1] ?? "",
  );
}

const css = readFileSync(GLOBALS_CSS_PATH, "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const keyframes = new Set(
  [...css.matchAll(/@keyframes\s+([\w-]+)/g)].map((match) => match[1]),
);

describe("the checker itself", () => {
  it("catches the old blanket rule", () => {
    const blanket = `@media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
    }`;
    const [block] = reduceBlocks(blanket);
    assert.deepEqual(stoppers(block ?? ""), [
      "a single iteration",
      "an animation shorter than 50 ms",
      "a transition shorter than 50 ms",
    ]);
  });

  it("catches an animation or transition switched off", () => {
    assert.deepEqual(stoppers("animation: none;"), ["animation: none"]);
    assert.deepEqual(stoppers("transition: none;"), ["transition: none"]);
  });

  it("lets a slower animation and a short colour transition through", () => {
    assert.deepEqual(
      stoppers("animation: spinner-pulse 1.6s ease-in-out infinite;"),
      [],
    );
    assert.deepEqual(stoppers("transition: color 120ms ease-out;"), []);
  });
});

describe("globals.css under prefers-reduced-motion: reduce", () => {
  it("has reduce blocks to check", () => {
    assert.ok(reduceBlocks(css).length >= MUST_KEEP_MOVING.length);
  });

  it("no reduce block switches motion off", () => {
    for (const block of reduceBlocks(css)) {
      assert.deepEqual(stoppers(block), [], block.trim());
    }
  });

  for (const utility of MUST_KEEP_MOVING) {
    it(`${utility} plays a defined animation for both values`, () => {
      const body = utilityBody(css, utility);
      assert.ok(body, `globals.css defines @utility ${utility}`);
      const [reduced, ...extra] = reduceBlocks(body);
      assert.ok(reduced, `${utility} has a reduce block`);
      assert.equal(extra.length, 0, `${utility} has one reduce block`);

      const fullBody = body.replace(reduced, "");
      for (const [label, block] of [
        ["no-preference", fullBody],
        ["reduce", reduced],
      ] as const) {
        const names = animationNames(block);
        assert.equal(
          names.length,
          1,
          `${utility} (${label}) sets one animation`,
        );
        assert.ok(
          keyframes.has(names[0]),
          `${utility} (${label}) plays @keyframes ${String(names[0])}, which is defined`,
        );
      }
    });
  }
});
