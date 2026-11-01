import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";

describe("shared types", () => {
  it("accept their documented forms and refuse the others", () => {
    assert.ok(api.Principal.safeParse({ type: "everyone", name: "*" }).success);
    assert.equal(
      api.Principal.safeParse({ type: "robot", name: "x" }).success,
      false,
    );
    assert.ok(
      api.EgressRule.safeParse({
        action: "deny",
        cidr: "@container_ip/32",
        port: "any",
        comment: "",
      }).success,
    );
    assert.equal(
      api.EgressRule.safeParse({
        action: "drop",
        cidr: "10.0.0.0/8",
        port: "any",
        comment: "",
      }).success,
      false,
    );
  });

  it("require exactly one of value and value_from in an env entry", () => {
    assert.ok(api.EnvEntry.safeParse({ name: "A", value: "1" }).success);
    assert.ok(
      api.EnvEntry.safeParse({ name: "A", value: null, value_from: "key" })
        .success,
    );
    assert.equal(api.EnvEntry.safeParse({ name: "A" }).success, false);
    assert.equal(
      api.EnvEntry.safeParse({ name: "A", value: "1", value_from: "key" })
        .success,
      false,
    );
  });
});
