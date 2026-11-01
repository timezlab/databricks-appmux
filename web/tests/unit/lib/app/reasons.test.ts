import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { unavailableReasons } from "@/lib/app/reasons";

describe("unavailableReasons", () => {
  it("says nothing when every action is on", () => {
    assert.deepEqual(
      unavailableReasons([
        { label: "Update", availability: { isAllowed: true } },
        { label: "Restart", availability: { isAllowed: true } },
      ]),
      [],
    );
  });

  it("names each refused action with its reason, in the order given", () => {
    assert.deepEqual(
      unavailableReasons([
        { label: "Update", availability: { isAllowed: true } },
        {
          label: "Delete",
          availability: { isAllowed: false, reason: "Locked." },
        },
        {
          label: "Activate",
          availability: { isAllowed: false, reason: "Already active." },
        },
      ]),
      ["Delete: Locked.", "Activate: Already active."],
    );
  });

  it("says a shared reason once, naming every action it holds back", () => {
    const reason = "Checking what you may do here...";
    assert.deepEqual(
      unavailableReasons([
        { label: "Activate", availability: { isAllowed: false, reason } },
        { label: "Lock", availability: { isAllowed: false, reason } },
        { label: "Delete", availability: { isAllowed: false, reason } },
      ]),
      [`Activate, Lock and Delete: ${reason}`],
    );
    assert.deepEqual(
      unavailableReasons([
        { label: "Start", availability: { isAllowed: false, reason } },
        { label: "Restart", availability: { isAllowed: false, reason } },
      ]),
      [`Start and Restart: ${reason}`],
    );
  });
});
