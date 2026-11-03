import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { REASON_NOT_MANAGER } from "@/lib/app/actions";
import type { Actor } from "@/lib/app/actions";
import { releaseMenuItems } from "@/lib/app/releases";
import type { ReleaseMenuItem } from "@/lib/app/releases";
import { readExample } from "../../../support/contract-examples";

const EXAMPLE_APP = api.AppDetail.parse(readExample("AppDetail"));

const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const VISITOR: Actor = {
  email: "bob@example.com",
  isPlatformAdmin: false,
  permission: "CAN_USE",
  canManage: false,
};

function exampleRelease(releaseNo: number): api.ReleaseSummary {
  const found = EXAMPLE_APP.releases.find(
    (release) => release.release_no === releaseNo,
  );
  assert.ok(found, `the example holds release ${String(releaseNo)}`);
  return found;
}

function itemsOf(releaseNo: number, actor: Actor | null = MANAGER) {
  return releaseMenuItems(exampleRelease(releaseNo), EXAMPLE_APP, actor);
}

function item(
  items: readonly ReleaseMenuItem[],
  action: ReleaseMenuItem["action"],
): ReleaseMenuItem {
  const found = items.find((entry) => entry.action === action);
  assert.ok(found, `the menu has ${action}`);
  return found;
}

function reasonOf(entry: ReleaseMenuItem): string {
  assert.equal(entry.availability.isAllowed, false, "expected a refusal");
  return entry.availability.isAllowed ? "" : entry.availability.reason;
}

describe("releaseMenuItems", () => {
  it("lists the actions in one order, Delete last", () => {
    assert.deepEqual(
      itemsOf(6).map((entry) => entry.label),
      ["Activate", "Rename", "Tag", "Lock", "View source", "Delete"],
    );
  });

  it("offers Unlock instead of Lock for a release locked by hand, and refuses Delete until then", () => {
    const items = itemsOf(5);
    assert.deepEqual(
      items.map((entry) => entry.action),
      ["activate", "rename", "tag", "unlock", "view-source", "delete"],
    );
    assert.equal(item(items, "unlock").availability.isAllowed, true);
    assert.match(reasonOf(item(items, "delete")), /unlock/i);
  });

  it("refuses Delete on the active release and says why", () => {
    const items = itemsOf(7);
    assert.match(reasonOf(item(items, "delete")), /active release/i);
    // Release 7 is active but no longer the target: activating it again rolls back the failed 8.
    assert.equal(item(items, "activate").availability.isAllowed, true);
  });

  it("says Activate rolls back to an older release, and retries a failed one", () => {
    assert.match(item(itemsOf(6), "activate").hint ?? "", /rolls back/i);
    assert.match(item(itemsOf(8), "activate").hint ?? "", /again/i);
  });

  it("marks Delete as the one destructive action", () => {
    assert.deepEqual(
      itemsOf(6)
        .filter((entry) => entry.isDestructive)
        .map((entry) => entry.action),
      ["delete"],
    );
  });

  it("refuses everything to a person who does not manage the app", () => {
    for (const entry of itemsOf(6, VISITOR)) {
      assert.equal(reasonOf(entry), REASON_NOT_MANAGER);
    }
  });
});
