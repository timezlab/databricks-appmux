/**
 * Guard over the CSS that `next build` ships. The edge serves the admin UI
 * under a strict CSP (FR-021, spike 19): `style-src 'self'`, `font-src 'self'`,
 * `img-src 'self'`. So the stylesheet must be a file of this origin that asks
 * the browser for nothing else. The source of the file is checked in
 * tests/contract/contrast.test.ts; this checks what Tailwind and the bundler
 * made of it. Run `npm run build` first (the "export" project reads `out/`).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import { EXPORT_DIR, exportFiles } from "../support/export-dir";

const CSS_FILES = exportFiles(".css");

/** What a stylesheet may not contain under the CSP, as the reason and the pattern. */
const FORBIDDEN: readonly (readonly [reason: string, pattern: RegExp])[] = [
  ["an @import (a second request)", /@import/i],
  ["a url() (an image, font or other request)", /url\(/i],
  ["an @font-face (a font file)", /@font-face/i],
  ["a data: URI", /data:/i],
  ["an absolute http(s) address", /https?:\/\//i],
  ["an IE expression()", /expression\(/i],
];

function violations(css: string): string[] {
  return FORBIDDEN.flatMap(([reason, pattern]) =>
    pattern.test(css) ? [reason] : [],
  );
}

describe("the guard itself", () => {
  it.each([
    ['@import "x.css";', ["an @import (a second request)"]],
    ["a{background:url(x.png)}", ["a url() (an image, font or other request)"]],
    ["@font-face{font-family:x}", ["an @font-face (a font file)"]],
    ["a{content:'data:text/plain,x'}", ["a data: URI"]],
    ["a{b:c} /* https://example.invalid/ */", ["an absolute http(s) address"]],
  ])("catches %s", (css, reasons) => {
    assert.deepEqual(violations(css), reasons);
  });

  it("lets ordinary rules through", () => {
    assert.deepEqual(
      violations(":root{--background:#fff}a{color:var(--link)}"),
      [],
    );
  });
});

describe("the shipped stylesheets", () => {
  it("exist", () => {
    assert.ok(CSS_FILES.length > 0, `no .css under ${EXPORT_DIR}`);
  });

  for (const file of CSS_FILES) {
    it(`${path.relative(EXPORT_DIR, file)} requests nothing`, () => {
      assert.deepEqual(violations(readFileSync(file, "utf8")), []);
    });
  }

  it("one of them carries the theme, with the dark override", () => {
    const themed = CSS_FILES.map((file) => readFileSync(file, "utf8")).filter(
      (css) => css.includes("--background:"),
    );
    assert.equal(themed.length, 1, "exactly one stylesheet defines the tokens");
    assert.match(themed[0] ?? "", /prefers-color-scheme:\s*dark/);
  });
});
