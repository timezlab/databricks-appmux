import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";

describe("ApiError", () => {
  it("is built from the contract's error body", () => {
    const error = api.ApiError.fromBody(409, {
      error: { code: "stale_version", message: "reload and retry" },
    });
    assert.ok(error instanceof Error);
    assert.equal(error.status, 409);
    assert.equal(error.code, "stale_version");
    assert.equal(error.message, "reload and retry");
    assert.equal(error.name, "ApiError");
  });
});

describe("httpStatusCode", () => {
  it("names the code of a failure the server answered without an error body", () => {
    assert.equal(api.httpStatusCode(502), "http_502");
    assert.equal(api.httpStatusCode(404), "http_404");
  });
});
