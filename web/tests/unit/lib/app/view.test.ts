import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { appLoadView } from "@/lib/app/view";
import { readExample } from "../../../support/contract-examples";

const APP = api.AppDetail.parse(readExample("AppDetail"));

function apiError(status: number): api.ApiError {
  return new api.ApiError(status, `http_${status}`, "Refused.");
}

describe("appLoadView", () => {
  it("waits while nothing has arrived", () => {
    assert.deepEqual(appLoadView({ status: "loading" }), { kind: "loading" });
  });

  it("shows the app, with no problem, when the call succeeded", () => {
    assert.deepEqual(
      appLoadView({ status: "ready", data: APP, updatedAtMs: 0 }),
      { kind: "ready", app: APP, refreshError: null },
    );
  });

  it("keeps the app on screen when a later call fails, and carries the error", () => {
    const error = apiError(503);
    assert.deepEqual(
      appLoadView({ status: "stale", data: APP, updatedAtMs: 0, error }),
      { kind: "ready", app: APP, refreshError: error },
    );
  });

  it("says not found for a 404 and no access for a 403", () => {
    assert.deepEqual(appLoadView({ status: "failed", error: apiError(404) }), {
      kind: "not-found",
    });
    assert.deepEqual(appLoadView({ status: "failed", error: apiError(403) }), {
      kind: "no-access",
    });
  });

  it("leaves other failures to the error notice", () => {
    const error = apiError(500);
    assert.deepEqual(appLoadView({ status: "failed", error }), {
      kind: "failed",
      error,
    });
    const network = new Error("offline");
    assert.deepEqual(appLoadView({ status: "failed", error: network }), {
      kind: "failed",
      error: network,
    });
  });

  it("drops the app once it is gone (a stale 404 means it was deleted)", () => {
    assert.deepEqual(
      appLoadView({
        status: "stale",
        data: APP,
        updatedAtMs: 0,
        error: apiError(404),
      }),
      { kind: "not-found" },
    );
  });

  it("drops the app once access is withdrawn (a stale 403)", () => {
    assert.deepEqual(
      appLoadView({
        status: "stale",
        data: APP,
        updatedAtMs: 0,
        error: apiError(403),
      }),
      { kind: "no-access" },
    );
  });
});
