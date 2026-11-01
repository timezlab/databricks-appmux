import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { SettingKey } from "@/lib/api";
import {
  PLATFORM_PAGES,
  SETTING_GROUPS,
  platformNavItems,
} from "@/lib/platform/sections";
import { SETTING_DESCRIPTORS } from "@/lib/platform/settings";

describe("platformNavItems", () => {
  it("lists the five pages in order, Overview first", () => {
    assert.deepEqual(
      platformNavItems(null).map((item) => item.label),
      ["Overview", "Apps", "Access", "Egress", "Settings"],
    );
    assert.equal(PLATFORM_PAGES[0]?.href, "/platform/");
  });

  it("shows how many requests wait on Overview, and nothing else", () => {
    const items = platformNavItems(3);
    assert.deepEqual(items[0]?.badge, {
      text: "3",
      description: "3 requests waiting",
    });
    assert.deepEqual(
      items.slice(1).map((item) => item.badge),
      [null, null, null, null],
    );
    assert.deepEqual(
      platformNavItems(1)[0]?.badge?.description,
      "1 request waiting",
    );
  });

  it("shows no badge while the requests are unknown or none wait", () => {
    assert.equal(platformNavItems(null)[0]?.badge, null);
    assert.equal(platformNavItems(0)[0]?.badge, null);
  });
});

describe("SETTING_GROUPS", () => {
  it("holds every setting exactly once, so none can be missing from the page", () => {
    const grouped = SETTING_GROUPS.flatMap((group) => group.keys).toSorted();
    assert.deepEqual(grouped, [...SettingKey.options].toSorted());
    assert.deepEqual(Object.keys(SETTING_DESCRIPTORS).toSorted(), grouped);
  });

  it("gives each group a unique anchor that matches its id", () => {
    assert.equal(
      new Set(SETTING_GROUPS.map((group) => group.id)).size,
      SETTING_GROUPS.length,
    );
    for (const group of SETTING_GROUPS) {
      assert.equal(group.href, `#${group.id}`);
    }
  });
});
