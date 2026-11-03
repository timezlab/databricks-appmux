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

  it("drops the C1 controls too, so a lone CSI (U+009B) cannot start a terminal sequence", () => {
    assert.equal(
      clipboardText("a\u0080b\u009b31mc\u009d0;title\u009cd\u009fe"),
      "ab31mc0;titlede",
    );
  });

  it("keeps U+00A0 and the first printable Latin-1 characters", () => {
    assert.equal(clipboardText("a\u00a0b\u00a1\u00e9"), "a\u00a0b\u00a1\u00e9");
  });

  it("keeps tab, line feed and carriage return, alone or before a line feed", () => {
    assert.equal(clipboardText("a\tb\nc\r\nd\re"), "a\tb\nc\r\nd\re");
  });

  it("leaves printable text alone, accents and emoji included", () => {
    assert.equal(clipboardText("café ✓ 日本 🚀 ~"), "café ✓ 日本 🚀 ~");
  });
});
