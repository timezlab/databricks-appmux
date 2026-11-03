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

  it("drops DEL (U+007F), which a shell without bracketed paste reads as a backspace", () => {
    assert.equal(
      clipboardText("rm -rf ./build/\u007f\u007f\u007f\n"),
      "rm -rf ./build/\n",
    );
  });

  it.each([
    ["U+202A, left-to-right embedding", "\u202a"],
    ["U+202B, right-to-left embedding", "\u202b"],
    ["U+202C, pop directional formatting", "\u202c"],
    ["U+202D, left-to-right override", "\u202d"],
    ["U+202E, right-to-left override", "\u202e"],
    ["U+2066, left-to-right isolate", "\u2066"],
    ["U+2067, right-to-left isolate", "\u2067"],
    ["U+2068, first strong isolate", "\u2068"],
    ["U+2069, pop directional isolate", "\u2069"],
  ])(
    "drops %s, so the pasted text reads in the order it runs",
    (_name, control) => {
      assert.equal(
        clipboardText(`rm ${control}fdp.txt${control} -i`),
        "rm fdp.txt -i",
      );
    },
  );

  it("keeps the implicit direction marks (U+200E, U+200F) and right-to-left words, which real text uses", () => {
    const arabic = "\u0645\u0631\u062d\u0628\u0627";
    const hebrew = "\u05e9\u05dc\u05d5\u05dd";
    const text = `${arabic}\u200e (ok) ${hebrew}\u200f.`;
    assert.equal(clipboardText(text), text);
  });

  it("keeps the line and paragraph separators (U+2028, U+2029)", () => {
    assert.equal(clipboardText("a\u2028b\u2029c"), "a\u2028b\u2029c");
  });

  it("keeps emoji built from several code points: a flag and a joined family", () => {
    const flag = "\u{1f1fb}\u{1f1f3}";
    const family = "\u{1f469}\u200d\u{1f469}\u200d\u{1f467}";
    assert.equal(clipboardText(`${flag} ${family}`), `${flag} ${family}`);
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
