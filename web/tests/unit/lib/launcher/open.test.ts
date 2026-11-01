import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { ApiError } from "@/lib/api";
import type { OpenModeRequest } from "@/lib/api";
import {
  OPEN_MODE_LABEL,
  openAppWithMode,
  openAsUserPrompt,
  openModeNeedsConfirmation,
} from "@/lib/launcher/open";

interface Calls {
  setMode: { slug: string; body: OpenModeRequest }[];
  navigate: string[];
}

function fakes(failure?: unknown) {
  const calls: Calls = { setMode: [], navigate: [] };
  const dependencies = {
    setMode: (slug: string, body: OpenModeRequest) => {
      calls.setMode.push({ slug, body });
      return failure === undefined
        ? Promise.resolve()
        : Promise.reject(failure instanceof Error ? failure : new Error("x"));
    },
    navigate: (href: string) => {
      calls.navigate.push(href);
    },
  };
  return { calls, dependencies };
}

describe("openAppWithMode", () => {
  it("saves the mode, then goes to the app", async () => {
    const { calls, dependencies } = fakes();
    const result = await openAppWithMode("sales", "as_user", dependencies);
    assert.deepEqual(result, { status: "opened" });
    assert.deepEqual(calls.setMode, [
      { slug: "sales", body: { mode: "as_user" } },
    ]);
    assert.deepEqual(calls.navigate, ["/sales/"]);
  });

  it("saves 'without token' too, so it switches back from an earlier 'as a user'", async () => {
    const { calls, dependencies } = fakes();
    await openAppWithMode("sales", "without_token", dependencies);
    assert.deepEqual(calls.setMode, [
      { slug: "sales", body: { mode: "without_token" } },
    ]);
  });

  it("does not navigate when the mode could not be saved (the old cookie may say 'as a user')", async () => {
    const failure = new ApiError(403, "forbidden", "not allowed");
    const { calls, dependencies } = fakes(failure);
    const result = await openAppWithMode(
      "sales",
      "without_token",
      dependencies,
    );
    assert.deepEqual(result, { status: "failed", error: failure });
    assert.deepEqual(calls.navigate, []);
  });

  it("calls nothing for a slug that is not a slug", async () => {
    const { calls, dependencies } = fakes();
    for (const slug of ["../admin", "_appmux", "", "a/b"]) {
      const result = await openAppWithMode(slug, "as_user", dependencies);
      assert.deepEqual(result, { status: "invalid-slug" }, slug);
    }
    assert.deepEqual(calls.setMode, []);
    assert.deepEqual(calls.navigate, []);
  });
});

describe("the words", () => {
  it("name the two modes as the design does", () => {
    assert.equal(OPEN_MODE_LABEL.without_token, "Open without token");
    assert.equal(OPEN_MODE_LABEL.as_user, "Open as a user");
  });

  it("the as-a-user question says whose token goes to the app and that it is recorded", () => {
    const prompt = openAsUserPrompt("Sales dashboard");
    assert.match(prompt, /Sales dashboard/);
    assert.match(prompt, /token/i);
    assert.match(prompt, /audit/i);
  });
});

describe("openModeNeedsConfirmation (the mode that hands over the admin's token)", () => {
  it("asks before 'as a user', which gives the app the token and writes an audit row", () => {
    assert.equal(openModeNeedsConfirmation("as_user"), true);
  });

  it("does not ask before 'without token', the default", () => {
    assert.equal(openModeNeedsConfirmation("without_token"), false);
  });
});
