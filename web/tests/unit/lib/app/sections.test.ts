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
  it("lists the ten sections of the app in order, each linking with the slug", () => {
    const items = appNavItems(healthyApp(), []);
    assert.deepEqual(
      items.map((item) => item.label),
      [
        "Overview",
        "Authorization",
        "Deployments",
        "Logs",
        "Security",
        "Environment",
        "Resources",
        "Insights",
        "Requests",
        "Settings",
      ],
    );
    assert.deepEqual(
      items.map((item) => item.href),
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
    assert.equal(Object.keys(APP_SECTION_LABEL).length, items.length);
  });

  it("gives each section its own icon, so a collapsed sidebar still tells them apart", () => {
    const icons = appNavItems(healthyApp(), []).map((item) => item.icon);
    assert.equal(new Set(icons).size, icons.length);
  });

  it("shows no count on a healthy app with nothing waiting, so a count always means something to do", () => {
    for (const item of appNavItems(healthyApp(), [])) {
      assert.equal(item.count, undefined, item.label);
    }
  });

  it("counts the notices on Overview and the waiting requests on Requests, and nothing elsewhere", () => {
    const crashed: api.AppDetail = { ...healthyApp(), state: "crashed" };
    const items = appNavItems(crashed, EXAMPLE_REQUESTS);
    const counted = items.filter((item) => item.count !== undefined);
    assert.deepEqual(
      counted.map((item) => item.label),
      ["Overview", "Requests"],
    );
    assert.ok((counted[0]?.count ?? 0) > 0);
    assert.equal(counted[1]?.count, 1);
  });

  it("says in words what each count means, and only beside a count", () => {
    const crashed: api.AppDetail = { ...healthyApp(), state: "crashed" };
    const items = appNavItems(crashed, EXAMPLE_REQUESTS);
    const overview = items.find((item) => item.label === "Overview");
    const requests = items.find((item) => item.label === "Requests");
    const noticeCount = overview?.count ?? 0;
    assert.equal(
      overview?.countDescription,
      noticeCount === 1
        ? "1 thing needs attention"
        : `${noticeCount} things need attention`,
    );
    assert.equal(requests?.countDescription, "1 waiting");
    for (const item of items.filter(
      (candidate) => candidate.count === undefined,
    )) {
      assert.equal(item.countDescription, undefined, item.label);
    }
  });

  it("uses the singular for exactly one notice", () => {
    // A crash alone is one notice on this fixture.
    const overview = appNavItems(
      { ...healthyApp(), state: "crashed" },
      [],
    ).find((item) => item.label === "Overview");
    assert.equal(overview?.count, 1);
    assert.equal(overview?.countDescription, "1 thing needs attention");
  });

  it("shows no requests count until the requests have been fetched", () => {
    const items = appNavItems(healthyApp(), null);
    assert.equal(
      items.find((item) => item.label === "Requests")?.count,
      undefined,
    );
  });
});
