import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  APP_SECTIONS,
  APP_SLUG_PARAMETER,
  appPagePath,
  appSectionAt,
  readAppParam,
} from "@/lib/app/params";

function search(query: string): URLSearchParams {
  return new URLSearchParams(query);
}

describe("readAppParam", () => {
  it("accepts a slug of the contract's pattern", () => {
    assert.deepEqual(readAppParam(search("slug=sales")), {
      status: "valid",
      slug: "sales",
    });
    assert.deepEqual(readAppParam(search("slug=my-app-2")), {
      status: "valid",
      slug: "my-app-2",
    });
  });

  it("reads no slug, or an empty one, as missing", () => {
    assert.deepEqual(readAppParam(search("")), { status: "missing" });
    assert.deepEqual(readAppParam(search("slug=")), { status: "missing" });
    assert.deepEqual(readAppParam(search("other=sales")), {
      status: "missing",
    });
  });

  it("refuses what is not a slug and keeps the text to show it", () => {
    for (const bad of [
      "Sales",
      "../hr",
      "sales/../hr",
      "_appmux",
      "my app",
      "x".repeat(32),
      "%2e%2e",
      "sales%0A",
    ]) {
      const result = readAppParam(search(`slug=${encodeURIComponent(bad)}`));
      assert.deepEqual(result, { status: "invalid", value: bad }, bad);
    }
  });

  it("takes the first slug when it is given twice", () => {
    assert.deepEqual(readAppParam(search("slug=a&slug=b")), {
      status: "valid",
      slug: "a",
    });
  });

  it("limits how much of a bad value is kept, so a huge query cannot flood the page", () => {
    const result = readAppParam(search(`slug=${"z".repeat(5000)}!`));
    assert.equal(result.status, "invalid");
    if (result.status === "invalid") {
      assert.ok(result.value.length <= 64);
    }
  });
});

describe("appPagePath", () => {
  it("builds the path other pages link to", () => {
    assert.equal(appPagePath("sales"), `/app/?${APP_SLUG_PARAMETER}=sales`);
  });

  it("builds the path of each page of the app, with the slug in the query", () => {
    assert.deepEqual(
      APP_SECTIONS.map((section) => appPagePath("sales", section)),
      [
        "/app/?slug=sales",
        "/app/authorization/?slug=sales",
        "/app/deployments/?slug=sales",
        "/app/logs/?slug=sales",
        "/app/security/?slug=sales",
        "/app/environment/?slug=sales",
        "/app/resources/?slug=sales",
        "/app/insights/?slug=sales",
        "/app/requests/?slug=sales",
        "/app/settings/?slug=sales",
      ],
    );
    assert.equal(appPagePath("sales", "overview"), appPagePath("sales"));
  });

  it("is null for a value that is not a slug, whatever the page", () => {
    assert.equal(appPagePath("../x"), null);
    assert.equal(appPagePath(""), null);
    assert.equal(appPagePath("../x", "deployments"), null);
  });

  it("reads back to the same slug", () => {
    const path = appPagePath("sales");
    assert.ok(path);
    const query = path.slice(path.indexOf("?") + 1);
    assert.deepEqual(readAppParam(new URLSearchParams(query)), {
      status: "valid",
      slug: "sales",
    });
  });
});

describe("appSectionAt", () => {
  it("names the section of each page's path, with or without its trailing slash", () => {
    assert.equal(appSectionAt("/app/"), "overview");
    assert.equal(appSectionAt("/app"), "overview");
    assert.equal(appSectionAt("/app/deployments/"), "deployments");
    assert.equal(appSectionAt("/app/deployments"), "deployments");
    assert.equal(appSectionAt("/app/settings/"), "settings");
  });

  it("reads back every path appPagePath builds", () => {
    for (const section of APP_SECTIONS) {
      const path = appPagePath("sales", section);
      assert.ok(path);
      assert.equal(appSectionAt(path.slice(0, path.indexOf("?"))), section);
    }
  });

  it("is null for a path that is no section of an app, including the old ones", () => {
    for (const path of [
      null,
      "",
      "/",
      "/platform/",
      "/app/releases/",
      "/app/sharing/",
      "/app/logs/extra/",
      "/apps/",
    ]) {
      assert.equal(appSectionAt(path), null, String(path));
    }
  });
});
