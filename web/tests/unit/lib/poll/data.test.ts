import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { pollData } from "@/lib/poll/data";
import type { PollResult } from "@/lib/poll/schedule";

describe("pollData", () => {
  it("is the last good value, also while a later call fails", () => {
    const ready: PollResult<number> = {
      status: "ready",
      data: 1,
      updatedAtMs: 0,
    };
    const stale: PollResult<number> = {
      status: "stale",
      data: 2,
      updatedAtMs: 0,
      error: new Error("x"),
    };
    assert.equal(pollData(ready), 1);
    assert.equal(pollData(stale), 2);
  });

  it("is the same object that was polled, not a copy", () => {
    const data = { items: [] };
    assert.equal(pollData({ status: "ready", data, updatedAtMs: 0 }), data);
  });

  it("is null before the first answer and after only failures", () => {
    assert.equal(pollData({ status: "loading" }), null);
    assert.equal(pollData({ status: "failed", error: new Error("x") }), null);
  });
});
