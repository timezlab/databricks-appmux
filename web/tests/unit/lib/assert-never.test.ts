import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { assertNever } from "@/lib/assert-never";

describe("assertNever", () => {
  it("throws an Error that shows the value no branch handled", () => {
    assert.throws(
      () => {
        assertNever("surprise" as never);
      },
      { name: "Error", message: /Unhandled value "surprise"/ },
    );
    assert.throws(
      () => {
        assertNever({ kind: "new" } as never);
      },
      { name: "Error", message: /Unhandled value \{"kind":"new"\}/ },
    );
  });
});
