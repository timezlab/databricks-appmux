/**
 * The admin UI plays the same animations whatever `prefers-reduced-motion` says
 * (docs/product/admin-ui-design.md, Motion; docs/decisions/0021). A person whose
 * system asks for less motion still saw a spinner that did not turn, because
 * the old `reduce` branch swapped the turn for a pulse, so the owner removed
 * the preference from the UI altogether. This reads the shipped source and
 * fails when a query, a `motion-reduce:` or `motion-safe:` utility, or a script
 * that reads the preference comes back, and when one of the animations that
 * say "something is happening" is not defined once, with keyframes that exist.
 * e2e/motion.spec.ts checks the same in Chromium under both values.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

const SOURCE_ROOT = path.resolve(import.meta.dirname, "../../src");
const GLOBALS_CSS_PATH = path.join(SOURCE_ROOT, "app/globals.css");

/** The utilities that say "something is happening"; each plays one animation, always. */
const ANIMATION_UTILITIES = [
  "animate-spinner",
  "animate-lamp-breathe",
  "animate-skeleton",
  "animate-menu-in",
  "animate-menu-out",
] as const;

/** The ones that repeat until the thing they stand for ends. */
const ENDLESS_UTILITIES = new Set([
  "animate-spinner",
  "animate-lamp-breathe",
  "animate-skeleton",
]);

/** Shorter than this, an animation is not seen: it is switched off. */
const SHORTEST_VISIBLE_MS = 50;

/** Everything the UI ships that could name the preference: styles, components and scripts. */
const SOURCE_EXTENSIONS = /\.(css|tsx?|jsx?|mjs)$/;

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(entryPath);
    return SOURCE_EXTENSIONS.test(entry.name) ? [entryPath] : [];
  });
}

function withoutComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "");
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

function utilityBody(css: string, name: string): string | null {
  const match = new RegExp(`@utility\\s+${name}\\s*\\{`).exec(css);
  return match ? blockBody(css, match.index + match[0].length - 1) : null;
}

const css = withoutComments(readFileSync(GLOBALS_CSS_PATH, "utf8"));
const keyframeNames = [...css.matchAll(/@keyframes\s+([\w-]+)/g)].map(
  (match) => match[1] ?? "",
);

describe("the preference is not read anywhere", () => {
  const files = sourceFiles(SOURCE_ROOT);

  it("looks at the styles, the components and the scripts", () => {
    assert.ok(files.some((file) => file.endsWith("globals.css")));
    assert.ok(files.some((file) => file.endsWith("button.tsx")));
  });

  it("has no prefers-reduced-motion query, in a style or in a script", () => {
    const readers = files.filter((file) =>
      /prefers-reduced-motion/i.test(
        withoutComments(readFileSync(file, "utf8")),
      ),
    );
    assert.deepEqual(
      readers.map((file) => path.relative(SOURCE_ROOT, file)),
      [],
    );
  });

  it("has no motion-reduce: or motion-safe: utility", () => {
    const readers = files.filter((file) =>
      /\bmotion-(?:reduce|safe):/.test(readFileSync(file, "utf8")),
    );
    assert.deepEqual(
      readers.map((file) => path.relative(SOURCE_ROOT, file)),
      [],
    );
  });
});

describe("globals.css animations", () => {
  for (const utility of ANIMATION_UTILITIES) {
    it(`${utility} plays one defined animation`, () => {
      const body = utilityBody(css, utility);
      assert.ok(body, `globals.css defines @utility ${utility}`);

      const declarations = [...body.matchAll(/animation\s*:\s*([^;}]+)/g)];
      assert.equal(declarations.length, 1, `${utility} sets one animation`);
      const shorthand = declarations[0]?.[1] ?? "";

      const name = /^([\w-]+)/.exec(shorthand.trim())?.[1] ?? "";
      assert.ok(
        keyframeNames.includes(name),
        `${utility} plays @keyframes ${name}, which is defined`,
      );

      const [, amount, unit] = /(\d*\.?\d+)(ms|s)\b/.exec(shorthand) ?? [];
      const durationMs = Number(amount) * (unit === "s" ? 1000 : 1);
      assert.ok(
        durationMs >= SHORTEST_VISIBLE_MS,
        `${utility} lasts ${String(durationMs)} ms, long enough to be seen`,
      );

      assert.equal(
        /\binfinite\b/.test(shorthand),
        ENDLESS_UTILITIES.has(utility),
        `${utility} ${ENDLESS_UTILITIES.has(utility) ? "repeats" : "plays once"}`,
      );
    });
  }

  it("keeps no keyframes that no animation plays", () => {
    const played = new Set(
      [...css.matchAll(/animation\s*:\s*([\w-]+)/g)].map(
        (match) => match[1] ?? "",
      ),
    );
    assert.deepEqual(
      keyframeNames.filter((name) => !played.has(name)),
      [],
    );
  });

  it("turns the spinner", () => {
    assert.match(utilityBody(css, "animate-spinner") ?? "", /spinner-turn/);
  });
});
