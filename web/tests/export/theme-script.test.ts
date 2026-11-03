/**
 * The theme script in the finished export (ADR 0019): public/theme.js is copied
 * to the export root, and every page loads it from `<head>` as a blocking
 * classic script, so `data-theme` is on `<html>` before the first paint. An
 * `async`, `defer` or `type="module"` script would run after the page has
 * painted in the OS theme and flash. Run `npm run build` first.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import { BASE_PATH, EXPORT_DIR, exportFiles } from "../support/export-dir";

const HTML_FILES = exportFiles(".html");
const THEME_SCRIPT_SRC = `${BASE_PATH}/theme.js`;

/** The opening tags of the scripts that load the theme file. */
function themeScriptTags(html: string): string[] {
  return (html.match(/<script\b[^>]*>/gi) ?? []).filter((tag) =>
    tag.includes(`src="${THEME_SCRIPT_SRC}"`),
  );
}

describe("the theme script", () => {
  it("is in the export, unchanged", () => {
    assert.equal(
      readFileSync(path.join(EXPORT_DIR, "theme.js"), "utf8"),
      readFileSync(
        path.resolve(import.meta.dirname, "../../public/theme.js"),
        "utf8",
      ),
    );
  });

  it("has pages to check", () => {
    assert.ok(HTML_FILES.length > 0, `no .html under ${EXPORT_DIR}`);
  });

  for (const file of HTML_FILES) {
    it(`${path.relative(EXPORT_DIR, file)} loads it once, blocking, in <head>`, () => {
      const html = readFileSync(file, "utf8");
      const head = html.slice(0, html.indexOf("</head>"));
      const tags = themeScriptTags(html);
      assert.equal(tags.length, 1, tags.join("\n"));
      const [tag = ""] = tags;
      assert.ok(head.includes(tag), "the script is not inside <head>");
      assert.doesNotMatch(tag, /\b(async|defer|type=)/i);
    });
  }
});
