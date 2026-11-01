import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  APP_SECTION_LABEL,
  appNavItems,
  pendingChangeRequestCount,
} from "@/lib/app/sections";
import { readExample } from "../../../support/contract-examples";

const EXAMPLE_APP = api.AppDetail.parse(readExample("AppDetail"));
const EXAMPLE_REQUESTS = api.RequestList.parse(
  readExample("RequestList"),
).items;

function healthyApp(): api.AppDetail {
  return {
    ...EXAMPLE_APP,
    state: "running",
    restart_to_apply: false,
    requirements: { ...EXAMPLE_APP.requirements, items: [] },
    releases: EXAMPLE_APP.releases.filter(
      (release) => release.status === "active",
    ),
  };
}

describe("pendingChangeRequestCount", () => {
  it("counts the pending settings and delegation requests of this app only", () => {
    assert.equal(pendingChangeRequestCount(EXAMPLE_REQUESTS, "sales"), 1);
    assert.equal(pendingChangeRequestCount(EXAMPLE_REQUESTS, "docs"), 1);
    assert.equal(pendingChangeRequestCount(EXAMPLE_REQUESTS, "nothing"), 0);
  });

  it("leaves out the answered ones, and the register request that belongs to the launcher", () => {
    const answered = EXAMPLE_REQUESTS.filter((r) => r.status !== "pending");
    assert.equal(pendingChangeRequestCount(answered, "docs"), 0);
    const register = EXAMPLE_REQUESTS.filter((r) => r.kind === "register");
    assert.equal(pendingChangeRequestCount(register, "docs"), 0);
  });
});

describe("appNavItems", () => {
  it("lists the five pages of the app in order, each linking with the slug", () => {
    const items = appNavItems(healthyApp(), []);
    assert.deepEqual(
      items.map((item) => item.label),
      ["Status", "Releases", "Resources", "Sharing", "Requests"],
    );
    assert.deepEqual(
      items.map((item) => item.href),
      [
        "/app/?slug=sales",
        "/app/releases/?slug=sales",
        "/app/resources/?slug=sales",
        "/app/sharing/?slug=sales",
        "/app/requests/?slug=sales",
      ],
    );
    assert.equal(Object.keys(APP_SECTION_LABEL).length, items.length);
  });

  it("shows no badge on a healthy app with nothing waiting, so a badge always means something to do", () => {
    for (const item of appNavItems(healthyApp(), [])) {
      assert.equal(item.badge, null, item.label);
    }
  });

  it("counts the notices on Status and the waiting requests on Requests", () => {
    const crashed: api.AppDetail = { ...healthyApp(), state: "crashed" };
    const items = appNavItems(crashed, EXAMPLE_REQUESTS);
    const status = items.find((item) => item.label === "Status");
    const requests = items.find((item) => item.label === "Requests");
    assert.ok(status?.badge);
    assert.match(status.badge.description, /need|needs/);
    assert.deepEqual(requests?.badge, { text: "1", description: "1 waiting" });
  });

  it("shows no requests badge until the requests have been fetched", () => {
    const items = appNavItems(healthyApp(), null);
    assert.equal(items.find((item) => item.label === "Requests")?.badge, null);
  });
});
