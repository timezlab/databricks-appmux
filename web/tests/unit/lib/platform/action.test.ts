import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  IDLE_ACTION,
  actionReducer,
  type ActionOutcome,
  type ActionState,
} from "@/lib/platform/action";

const OUTCOME: ActionOutcome = { tone: "success", title: "Saved" };

describe("actionReducer", () => {
  it("goes idle, busy, done", () => {
    const busy = actionReducer(IDLE_ACTION, { type: "started" });
    assert.deepEqual(busy, { status: "busy" });
    assert.deepEqual(
      actionReducer(busy, { type: "succeeded", outcome: OUTCOME }),
      {
        status: "done",
        outcome: OUTCOME,
      },
    );
  });

  it("keeps the error a call threw so the notice can word it by status", () => {
    const error = new Error("conflict");
    const busy = actionReducer(IDLE_ACTION, { type: "started" });
    assert.deepEqual(actionReducer(busy, { type: "failed", error }), {
      status: "failed",
      error,
    });
  });

  it("ignores a second start while a call is in flight, so a double click sends one write", () => {
    const busy: ActionState = { status: "busy" };
    assert.equal(actionReducer(busy, { type: "started" }), busy);
  });

  it("starts again after a result, and the old result is gone", () => {
    const done: ActionState = { status: "done", outcome: OUTCOME };
    assert.deepEqual(actionReducer(done, { type: "started" }), {
      status: "busy",
    });
    const failed: ActionState = { status: "failed", error: new Error("x") };
    assert.deepEqual(actionReducer(failed, { type: "started" }), {
      status: "busy",
    });
  });

  it("ignores a result that arrives when no call is in flight", () => {
    assert.equal(
      actionReducer(IDLE_ACTION, { type: "succeeded", outcome: OUTCOME }),
      IDLE_ACTION,
    );
    assert.equal(
      actionReducer(IDLE_ACTION, { type: "failed", error: new Error("x") }),
      IDLE_ACTION,
    );
    const done: ActionState = { status: "done", outcome: OUTCOME };
    assert.equal(
      actionReducer(done, { type: "failed", error: new Error("x") }),
      done,
    );
  });

  it("clears a shown result on request, but not a call in flight", () => {
    const done: ActionState = { status: "done", outcome: OUTCOME };
    assert.equal(actionReducer(done, { type: "cleared" }), IDLE_ACTION);
    const busy: ActionState = { status: "busy" };
    assert.equal(actionReducer(busy, { type: "cleared" }), busy);
  });
});
