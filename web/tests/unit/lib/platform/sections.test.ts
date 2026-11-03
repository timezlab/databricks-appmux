import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { SettingKey } from "@/lib/api";
import {
  PLATFORM_PAGES,
  SETTING_GROUPS,
  platformSidebarItems,
} from "@/lib/platform/sections";
import { SETTING_DESCRIPTORS } from "@/lib/platform/settings";

describe("platformSidebarItems", () => {
  it("lists the five pages in order, Overview first, each with its own icon", () => {
    const items = platformSidebarItems(null);
    assert.deepEqual(
      items.map((item) => item.label),
      ["Overview", "Apps", "Access", "Egress", "Settings"],
    );
    assert.equal(PLATFORM_PAGES[0]?.href, "/platform/");
    assert.equal(new Set(items.map((item) => item.icon)).size, items.length);
  });

  it("counts the requests waiting on Overview, and nothing else", () => {
    const items = platformSidebarItems(3);
    assert.equal(items[0]?.count, 3);
    assert.deepEqual(
      items.slice(1).map((item) => item.count),
      [undefined, undefined, undefined, undefined],
    );
  });

  it("shows no count while the requests are unknown or none wait", () => {
    assert.equal(platformSidebarItems(null)[0]?.count, undefined);
    assert.equal(platformSidebarItems(0)[0]?.count, undefined);
    assert.equal("count" in (platformSidebarItems(0)[0] ?? {}), false);
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
