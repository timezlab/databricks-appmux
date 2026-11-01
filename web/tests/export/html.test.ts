/**
 * Guard over the HTML that `next build` prerenders, page by page. The edge
 * serves it under a strict CSP (FR-021, spike 19: `style-src 'self'`,
 * `script-src 'self'` plus the hash of each page's inline scripts,
 * `base-uri 'none'`, `form-action 'none'`, `frame-ancestors 'none'`). The
 * components are checked in tests/csp/rendered-markup.test.tsx; this checks
 * the finished files, which also hold what Next adds around them. Every page
 * must:
 *  - carry no style attribute, `<style>`, event-handler attribute or
 *    `javascript:` URL (inline script bodies are not markup and are skipped);
 *  - link its CSS as a file of this origin (under the base path, present in
 *    the export), never inline;
 *  - load everything else (scripts, images, preloads) from this origin too;
 *  - have no `<base>`, `<iframe>`, `<object>` or `<embed>`.
 * Run `npm run build` first (the "export" project reads `out/`).
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import { BASE_PATH, EXPORT_DIR, exportFiles } from "../support/export-dir";

const HTML_FILES = exportFiles(".html");

/** The markup without the bodies of inline scripts and JSON (their text is data). */
function markupOnly(html: string): string {
  return html.replace(
    /<script\b[^>]*>[\s\S]*?<\/script>/gi,
    "<script></script>",
  );
}

function attribute(tag: string, name: string): string | null {
  const match = new RegExp(`\\s${name}\\s*=\\s*"([^"]*)"`, "i").exec(tag);
  return match?.[1] ?? null;
}

/** True for a path on this origin under the base path: not a scheme, not `//host`. */
function isOwnPath(reference: string): boolean {
  return reference.startsWith(`${BASE_PATH}/`) && !reference.includes("//", 1);
}

function violations(html: string): string[] {
  const markup = markupOnly(html);
  const found: string[] = [];
  const forbidden: readonly (readonly [string, RegExp])[] = [
    ["a style attribute", /\sstyle\s*=/i],
    ["a <style> element", /<style[\s>]/i],
    ["an event-handler attribute", /\son[a-z]+\s*=/i],
    ["a javascript: URL", /javascript:/i],
    [
      "a <base>, <iframe>, <object> or <embed>",
      /<(?:base|iframe|object|embed)[\s>/]/i,
    ],
  ];
  for (const [reason, pattern] of forbidden) {
    if (pattern.test(markup)) found.push(reason);
  }

  let stylesheets = 0;
  for (const tag of html.match(/<(?:link|script|img)\b[^>]*>/gi) ?? []) {
    const reference = attribute(
      tag,
      tag.startsWith("<script") ? "src" : /^<link/i.test(tag) ? "href" : "src",
    );
    if (reference === null) continue;
    if (!isOwnPath(reference))
      found.push(`${reference} is not a path of this origin`);
    if (/^<link\b/i.test(tag) && /\srel\s*=\s*"stylesheet"/i.test(tag)) {
      stylesheets += 1;
      if (
        !existsSync(path.join(EXPORT_DIR, reference.slice(BASE_PATH.length)))
      ) {
        found.push(`${reference} is not in the export`);
      }
    }
  }
  if (stylesheets === 0) found.push("no linked stylesheet");
  return found;
}

describe("the guard itself", () => {
  const css = `<link rel="stylesheet" href="${BASE_PATH}/_next/static/x.css"/>`;

  it("catches each way a page leaves the CSP", () => {
    for (const [html, reason] of [
      [`${css}<p style="x:y">`, "a style attribute"],
      [`${css}<style>p{}</style>`, "a <style> element"],
      [`${css}<a onclick="go()">`, "an event-handler attribute"],
      [`${css}<a href="javascript:go()">`, "a javascript: URL"],
      [`${css}<base href="/">`, "a <base>, <iframe>, <object> or <embed>"],
      [
        `${css}<script src="https://example.invalid/x.js"></script>`,
        "https://example.invalid/x.js is not a path of this origin",
      ],
      [
        `${css}<script src="//example.invalid/x.js"></script>`,
        "//example.invalid/x.js is not a path of this origin",
      ],
      ["<p>no css</p>", "no linked stylesheet"],
    ] as const) {
      assert.ok(violations(html).includes(reason), `${reason}: ${html}`);
    }
  });

  it("does not read the data inside an inline script as markup", () => {
    const html = `${css}<script>self.__next_f.push(["<p style=\\"a\\" onclick=\\"b\\">"])</script>`;
    assert.ok(
      !violations(html).some((reason) => reason.includes("attribute")),
      violations(html).join(", "),
    );
  });
});

describe("the prerendered pages", () => {
  it("exist", () => {
    assert.ok(HTML_FILES.length > 0, `no .html under ${EXPORT_DIR}`);
  });

  for (const file of HTML_FILES) {
    it(`${path.relative(EXPORT_DIR, file)} stays inside the CSP`, () => {
      assert.deepEqual(violations(readFileSync(file, "utf8")), []);
    });
  }
});
