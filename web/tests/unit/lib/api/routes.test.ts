import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { expandRoute } from "@/lib/api/routes";

describe("expandRoute: a numeric path id", () => {
  // admin-api.md: `{id}` is a release number or a request id. Both start at 1
  // (v1-data-model.md: `release_no > 0`, `request_id` identity), never 0.
  const release = (id: number) =>
    expandRoute("GET /apps/{app}/releases/{id}", { app: "sales", id });

  it("accepts whole numbers from 1 up to the largest safe integer", () => {
    assert.equal(release(1).url, "/_appmux/api/v1/apps/sales/releases/1");
    assert.equal(release(412).url, "/_appmux/api/v1/apps/sales/releases/412");
    assert.equal(
      release(Number.MAX_SAFE_INTEGER).url,
      `/_appmux/api/v1/apps/sales/releases/${Number.MAX_SAFE_INTEGER}`,
    );
  });

  it("refuses what could not be an id, so it never reaches the URL", () => {
    for (const bad of [
      Number.NaN,
      Number.POSITIVE_INFINITY,
      Number.NEGATIVE_INFINITY,
      1.5,
      0,
      -0,
      -1,
      Number.MAX_SAFE_INTEGER + 1,
    ]) {
      assert.throws(() => release(bad), /Invalid path segment/, String(bad));
    }
  });

  it("holds for a request id too", () => {
    assert.equal(
      expandRoute("POST /requests/{id}/approve", { id: 4 }).url,
      "/_appmux/api/v1/requests/4/approve",
    );
    assert.throws(
      () => expandRoute("POST /requests/{id}/approve", { id: Number.NaN }),
      /Invalid path segment/,
    );
  });
});
