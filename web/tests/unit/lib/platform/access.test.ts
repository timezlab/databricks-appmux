import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { MeResponse } from "@/lib/api/schemas";
import { platformAccess } from "@/lib/platform/access";
import type { PollResult } from "@/lib/poll/schedule";

function me(isPlatformAdmin: boolean): MeResponse {
  return {
    email: "user@example.com",
    groups: [],
    is_platform_admin: isPlatformAdmin,
  };
}

describe("platformAccess", () => {
  it("waits while GET /me has not answered, and shows no page data meanwhile", () => {
    assert.deepEqual(platformAccess({ status: "loading" }), {
      state: "checking",
    });
  });

  it("lets a platform admin in", () => {
    assert.deepEqual(
      platformAccess({ status: "ready", data: me(true), updatedAtMs: 1 }),
      { state: "allowed", email: "user@example.com" },
    );
  });

  it("shows the no-access state to anyone else, naming who is signed in", () => {
    assert.deepEqual(
      platformAccess({ status: "ready", data: me(false), updatedAtMs: 1 }),
      { state: "denied", email: "user@example.com" },
    );
  });

  it("does not open the page when GET /me failed: not knowing is not permission", () => {
    const error = new Error("offline");
    assert.deepEqual(platformAccess({ status: "failed", error }), {
      state: "error",
      error,
    });
  });

  it("keeps the last answer when a later poll of GET /me fails", () => {
    const stale = (data: MeResponse): PollResult<MeResponse> => ({
      status: "stale",
      data,
      updatedAtMs: 1,
      error: new Error("offline"),
    });
    assert.equal(platformAccess(stale(me(true))).state, "allowed");
    assert.equal(platformAccess(stale(me(false))).state, "denied");
  });
});
