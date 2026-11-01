import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  NAV_ITEMS,
  isCurrentNavEntry,
  isCurrentNavItem,
  isCurrentSubNavItem,
} from "@/components/nav-items";

describe("NAV_ITEMS", () => {
  it("lists the apps (launcher) page, the platform page and the dashboard, as the screens of design 10.1", () => {
    assert.deepEqual(
      NAV_ITEMS.map((item) => item.label),
      ["Apps", "Platform", "Dashboard"],
    );
  });

  it("links to the page directories of the export (trailingSlash), for next/link to prefix with the base path", () => {
    assert.deepEqual(
      NAV_ITEMS.map((item) => item.href),
      ["/", "/platform/", "/dashboard/"],
    );
  });
});

describe("isCurrentNavItem", () => {
  it("marks the launcher only on the root", () => {
    assert.equal(isCurrentNavItem("/", "/"), true);
    assert.equal(isCurrentNavItem("/platform/", "/"), false);
    assert.equal(isCurrentNavItem("/app/", "/"), false);
  });

  it("marks a page whatever the trailing slash", () => {
    assert.equal(isCurrentNavItem("/platform/", "/platform/"), true);
    assert.equal(isCurrentNavItem("/platform", "/platform/"), true);
    assert.equal(isCurrentNavItem("/dashboard/", "/platform/"), false);
  });

  it("marks a page for what lies under it, not for a name that merely starts the same", () => {
    assert.equal(isCurrentNavItem("/platform/egress/", "/platform/"), true);
    assert.equal(isCurrentNavItem("/platformer/", "/platform/"), false);
  });

  it("marks nothing when the path is unknown", () => {
    assert.equal(isCurrentNavItem(null, "/"), false);
    assert.equal(isCurrentNavItem("", "/platform/"), false);
  });
});

describe("isCurrentNavEntry", () => {
  const apps = NAV_ITEMS.find((item) => item.label === "Apps");
  const platform = NAV_ITEMS.find((item) => item.label === "Platform");

  it("keeps the Apps tab lit on the pages of one app, which are part of the Apps screen", () => {
    assert.ok(apps !== undefined);
    assert.equal(isCurrentNavEntry("/", apps), true);
    assert.equal(isCurrentNavEntry("/app/", apps), true);
    assert.equal(isCurrentNavEntry("/app/releases/", apps), true);
    assert.equal(isCurrentNavEntry("/app/requests", apps), true);
  });

  it("does not light Apps on the other screens, nor a path that merely starts with app", () => {
    assert.ok(apps !== undefined);
    assert.equal(isCurrentNavEntry("/platform/", apps), false);
    assert.equal(isCurrentNavEntry("/dashboard/", apps), false);
    assert.equal(isCurrentNavEntry("/application/", apps), false);
  });

  it("does not light the other tabs on an app page", () => {
    assert.ok(platform !== undefined);
    assert.equal(isCurrentNavEntry("/app/", platform), false);
    assert.equal(isCurrentNavEntry("/platform/apps/", platform), true);
  });
});

describe("isCurrentSubNavItem", () => {
  it("lights a tab only on its own page, so the first tab does not stay lit on the others", () => {
    assert.equal(isCurrentSubNavItem("/platform/", "/platform/"), true);
    assert.equal(isCurrentSubNavItem("/platform/apps/", "/platform/"), false);
    assert.equal(
      isCurrentSubNavItem("/platform/apps/", "/platform/apps/"),
      true,
    );
  });

  it("ignores the trailing slash and the query string of the tab's link", () => {
    assert.equal(isCurrentSubNavItem("/app", "/app/?slug=sales"), true);
    assert.equal(
      isCurrentSubNavItem("/app/releases/", "/app/releases/?slug=sales"),
      true,
    );
    assert.equal(
      isCurrentSubNavItem("/app/", "/app/releases/?slug=sales"),
      false,
    );
  });

  it("lights nothing when the path is unknown", () => {
    assert.equal(isCurrentSubNavItem(null, "/platform/"), false);
    assert.equal(isCurrentSubNavItem("", "/platform/"), false);
  });
});
