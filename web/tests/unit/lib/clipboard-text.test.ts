import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { clipboardText } from "@/lib/clipboard-text";

const ESC = "\u001b";

describe("clipboardText", () => {
  it("drops ESC, so a pasted log line cannot drive a terminal", () => {
    assert.equal(
      clipboardText(`${ESC}[31merror${ESC}[0m ${ESC}]0;title\u0007`),
      "[31merror[0m ]0;title",
    );
  });

  it("drops every other C0 control character", () => {
    assert.equal(
      clipboardText("a\u0000b\u0001c\u0008d\u000be\u000cf\u000eg\u001fh"),
      "abcdefgh",
    );
  });

  it("keeps tab, line feed and carriage return, alone or before a line feed", () => {
    assert.equal(clipboardText("a\tb\nc\r\nd\re"), "a\tb\nc\r\nd\re");
  });

  it("leaves printable text alone, accents and emoji included", () => {
    assert.equal(clipboardText("café ✓ 日本 🚀 ~"), "café ✓ 日本 🚀 ~");
  });
});
