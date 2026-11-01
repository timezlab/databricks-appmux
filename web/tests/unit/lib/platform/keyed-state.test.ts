import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { keyedValue, writeKeyed } from "@/lib/platform/keyed-state";

describe("keyedValue", () => {
  it("returns what was written under the same key", () => {
    const held = writeKeyed(
      { key: "a", value: "start" },
      "a",
      "start",
      "typed",
    );
    assert.equal(keyedValue(held, "a", "start"), "typed");
  });

  it("returns the new initial value once the key has changed, so a form takes the server's new values", () => {
    const held = { key: "a", value: "typed" };
    assert.equal(keyedValue(held, "b", "from the server"), "from the server");
  });
});

describe("writeKeyed", () => {
  it("hands an updater the initial value, not the old key's value, after the key changed", () => {
    const held = { key: "a", value: 10 };
    const next = writeKeyed(held, "b", 1, (previous) => previous + 1);
    assert.deepEqual(next, { key: "b", value: 2 });
  });

  it("hands an updater the held value while the key is the same", () => {
    const held = { key: "a", value: 10 };
    const next = writeKeyed(held, "a", 1, (previous) => previous + 1);
    assert.deepEqual(next, { key: "a", value: 11 });
  });

  it("stores a plain value under the current key", () => {
    const next = writeKeyed({ key: "a", value: "old" }, "b", "init", "new");
    assert.deepEqual(next, { key: "b", value: "new" });
  });
});
