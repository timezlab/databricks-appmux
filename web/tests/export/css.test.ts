/**
 * Guard over the CSS that `next build` ships. The edge serves the admin UI
 * under a strict CSP (FR-021, spike 19): `style-src 'self'`, `font-src 'self'`,
 * `img-src 'self'`. So the stylesheet must be a file of this origin that asks
 * the browser for nothing but the export's own font files. The source of the
 * tokens is checked in tests/contract/contrast.test.ts; this checks what
 * Tailwind and the bundler made of it. Run `npm run build` first (the "export"
 * project reads `out/`).
 *
 * The one request a stylesheet may make is an `@font-face` source: IBM Plex,
 * self-hosted (src/app/fonts.css). So `url(` is allowed only when its target is
 * a relative `.woff2` path, and in the shipped files that path must name a file
 * of the export. Everything else (an image, a `data:` font, another origin, an
 * `@import`) is still refused.
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import { EXPORT_DIR, exportFiles } from "../support/export-dir";

const CSS_FILES = exportFiles(".css");

/** What a stylesheet may not contain under the CSP, as the reason and the pattern. */
const FORBIDDEN: readonly (readonly [reason: string, pattern: RegExp])[] = [
  ["an @import (a second request)", /@import/i],
  ["a data: URI", /data:/i],
  ["an absolute http(s) address", /https?:\/\//i],
  ["an IE expression()", /expression\(/i],
];

const NOT_A_FONT_FILE =
  "a url() that is not a relative .woff2 (an image or other request)";

/** `url(x)`, `url('x')` or `url("x")`; the target is group 2. */
const URL_CALL = /url\(\s*(["']?)([^"')]*)\1\s*\)/gi;

/** The targets of every `url(` in `css`, or `null` for one this parser cannot read. */
function urlTargets(css: string): (string | null)[] {
  const parsed = [...css.matchAll(URL_CALL)].map((match) => match[2] ?? "");
  const opened = css.match(/url\(/gi)?.length ?? 0;
  return opened === parsed.length ? parsed : [...parsed, null];
}

/** A path relative to the stylesheet: no scheme, no host, not rooted, ends in `.woff2`. */
function isRelativeWoff2(target: string | null): target is string {
  if (target === null) return false;
  const hasScheme = /^[a-z][a-z0-9+.-]*:/i.test(target);
  return !hasScheme && !target.startsWith("/") && /\.woff2$/i.test(target);
}

function violations(css: string): string[] {
  const forbidden = FORBIDDEN.flatMap(([reason, pattern]) =>
    pattern.test(css) ? [reason] : [],
  );
  const isEveryUrlAFont = urlTargets(css).every(isRelativeWoff2);
  return isEveryUrlAFont ? forbidden : [...forbidden, NOT_A_FONT_FILE];
}

describe("the guard itself", () => {
  it.each([
    ['@import "x.css";', ["an @import (a second request)"]],
    ["a{background:url(x.png)}", [NOT_A_FONT_FILE]],
    ['@font-face{src:url("x.woff")}', [NOT_A_FONT_FILE]],
    ["@font-face{src:url(/media/x.woff2)}", [NOT_A_FONT_FILE]],
    ["@font-face{src:url(//example.invalid/x.woff2)}", [NOT_A_FONT_FILE]],
    [
      "@font-face{src:url(https://example.invalid/x.woff2)}",
      ["an absolute http(s) address", NOT_A_FONT_FILE],
    ],
    [
      "@font-face{src:url(data:font/woff2;base64,AAAA)}",
      ["a data: URI", NOT_A_FONT_FILE],
    ],
    ["a{content:'data:text/plain,x'}", ["a data: URI"]],
    ["a{b:c} /* https://example.invalid/ */", ["an absolute http(s) address"]],
  ])("catches %s", (css, reasons) => {
    assert.deepEqual(violations(css), reasons);
  });

  it.each([
    ":root{--background:#fff}a{color:var(--link)}",
    '@font-face{font-family:x;src:url(../media/x.0a1b.woff2) format("woff2")}',
    "@font-face{src:url('../media/x.woff2')}",
  ])("lets %s through", (css) => {
    assert.deepEqual(violations(css), []);
  });
});

describe("the shipped stylesheets", () => {
  it("exist", () => {
    assert.ok(CSS_FILES.length > 0, `no .css under ${EXPORT_DIR}`);
  });

  for (const file of CSS_FILES) {
    const name = path.relative(EXPORT_DIR, file);
    const css = readFileSync(file, "utf8");

    it(`${name} requests nothing but its own font files`, () => {
      assert.deepEqual(violations(css), []);
    });

    it(`every font file ${name} names is in the export`, () => {
      for (const target of urlTargets(css).filter(isRelativeWoff2)) {
        const resolved = path.resolve(path.dirname(file), target);
        assert.ok(
          resolved.startsWith(EXPORT_DIR + path.sep) && existsSync(resolved),
          `${name} points at ${target}, which is not a file of the export`,
        );
      }
    });
  }

  it("one of them carries the theme, with both dark overrides (the Dark choice, and System on a dark OS)", () => {
    const themed = CSS_FILES.map((file) => readFileSync(file, "utf8")).filter(
      (css) => css.includes("--background:"),
    );
    assert.equal(themed.length, 1, "exactly one stylesheet defines the tokens");
    assert.match(themed[0] ?? "", /prefers-color-scheme:\s*dark/);
    assert.match(themed[0] ?? "", /\[data-theme=["']?dark["']?\]/);
    assert.match(themed[0] ?? "", /:not\(\[data-theme=["']?light["']?\]\)/);
  });

  it("ship IBM Plex Sans and IBM Plex Mono", () => {
    const all = CSS_FILES.map((file) => readFileSync(file, "utf8")).join("\n");
    assert.match(all, /font-family:\s*["']?IBM Plex Sans/);
    assert.match(all, /font-family:\s*["']?IBM Plex Mono/);
  });
});
