import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { FORM_ERROR_KEY } from "@/lib/app/forms";
import {
  EMPTY_GRANT_FORM,
  buildGrantRequest,
  changeGrantType,
  grantLevelChange,
  grantLevelChangePrompt,
  grantRemoveAvailability,
  grantRemovePrompt,
  grantRows,
  permissionLabel,
} from "@/lib/app/sharing";
import { readExample } from "../../../support/contract-examples";

const APP = api.AppDetail.parse(readExample("AppDetail"));

const MANAGER: Actor = {
  email: "carol@example.com",
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

describe("grantRows", () => {
  it("lists the owner first, as a fixed row, then the grants in the order sent", () => {
    const rows = grantRows(APP);
    assert.deepEqual(
      rows.map((row) => [row.type, row.name, row.permission, row.isOwner]),
      [
        ["user", "alice@example.com", "CAN_MANAGE", true],
        ["user", "carol@example.com", "CAN_MANAGE", false],
        ["group", "sales-team", "CAN_USE", false],
      ],
    );
    assert.equal(rows[0]?.grantedBy, null);
    assert.equal(rows[1]?.grantedBy, "alice@example.com");
  });

  it("shows the owner once, even if a grant also names them", () => {
    const app = {
      ...APP,
      grants: [
        {
          type: "user" as const,
          name: "alice@example.com",
          permission: "CAN_MANAGE" as const,
          granted_by: "dana@example.com",
          granted_at: "2026-10-30T08:00:00Z",
        },
        ...APP.grants,
      ],
    };
    const owners = grantRows(app).filter((row) => row.isOwner);
    assert.equal(owners.length, 1);
    assert.equal(grantRows(app).length, 3);
  });

  it("gives every row a distinct id", () => {
    const ids = grantRows(APP).map((row) => row.id);
    assert.equal(new Set(ids).size, ids.length);
  });
});

describe("permissionLabel", () => {
  it("names the two levels in words", () => {
    assert.equal(permissionLabel("CAN_USE"), "Can use");
    assert.equal(permissionLabel("CAN_MANAGE"), "Can manage");
  });
});

describe("changeGrantType", () => {
  it("fixes everyone to * and CAN_USE", () => {
    const next = changeGrantType(
      { type: "user", name: "x@example.com", permission: "CAN_MANAGE" },
      "everyone",
    );
    assert.deepEqual(next, {
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
  });

  it("clears the * when leaving everyone, and keeps a typed name otherwise", () => {
    const everyone = changeGrantType(EMPTY_GRANT_FORM, "everyone");
    assert.equal(changeGrantType(everyone, "group").name, "");
    const typed = { ...EMPTY_GRANT_FORM, name: "sales-team" };
    assert.equal(changeGrantType(typed, "group").name, "sales-team");
  });
});

describe("buildGrantRequest", () => {
  it("builds a user grant with the email trimmed and lower-cased", () => {
    const result = buildGrantRequest(APP, {
      type: "user",
      name: "  Erin@Example.com ",
      permission: "CAN_USE",
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "user",
      name: "erin@example.com",
      permission: "CAN_USE",
    });
  });

  it("builds a group grant with the name as typed, trimmed", () => {
    const result = buildGrantRequest(APP, {
      type: "group",
      name: " Data Platform ",
      permission: "CAN_MANAGE",
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "group",
      name: "Data Platform",
      permission: "CAN_MANAGE",
    });
  });

  it("builds everyone as * with CAN_USE", () => {
    const result = buildGrantRequest(APP, {
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
  });

  it("refuses everyone with CAN_MANAGE", () => {
    const result = buildGrantRequest(APP, {
      type: "everyone",
      name: "*",
      permission: "CAN_MANAGE",
    });
    assert.ok(!result.ok);
    assert.match(result.errors.permission ?? "", /CAN_USE|only/i);
  });

  it("refuses something that is not an email, and a blank group", () => {
    const badUser = buildGrantRequest(APP, {
      type: "user",
      name: "carol",
      permission: "CAN_USE",
    });
    assert.ok(!badUser.ok);
    assert.match(badUser.errors.name ?? "", /email/i);

    const blankGroup = buildGrantRequest(APP, {
      type: "group",
      name: "   ",
      permission: "CAN_USE",
    });
    assert.ok(!blankGroup.ok);
    assert.match(blankGroup.errors.name ?? "", /enter/i);
  });

  it("refuses to give the owner a lower level, as the server does", () => {
    const result = buildGrantRequest(APP, {
      type: "user",
      name: "Alice@example.com",
      permission: "CAN_USE",
    });
    assert.ok(!result.ok);
    assert.match(result.errors.name ?? "", /owner/i);
  });

  it("keeps nothing under the form key when a field is wrong", () => {
    const result = buildGrantRequest(APP, {
      type: "user",
      name: "",
      permission: "CAN_USE",
    });
    assert.ok(!result.ok);
    assert.equal(result.errors[FORM_ERROR_KEY], undefined);
  });
});

describe("grantLevelChange", () => {
  const [, carol, team] = grantRows(APP);
  assert.ok(carol && team);

  it("offers the other level, as a request", () => {
    assert.deepEqual(grantLevelChange(carol), {
      label: "Make user",
      request: {
        type: "user",
        name: "carol@example.com",
        permission: "CAN_USE",
      },
    });
    assert.deepEqual(grantLevelChange(team), {
      label: "Make admin",
      request: { type: "group", name: "sales-team", permission: "CAN_MANAGE" },
    });
  });

  it("offers nothing for the owner or for everyone", () => {
    const [owner] = grantRows(APP);
    assert.ok(owner);
    assert.equal(grantLevelChange(owner), null);
    assert.equal(
      grantLevelChange({
        id: "everyone:*",
        type: "everyone",
        name: "*",
        permission: "CAN_USE",
        grantedBy: "x@example.com",
        grantedAt: "2026-10-30T08:00:00Z",
        isOwner: false,
      }),
      null,
    );
  });
});

describe("grantLevelChangePrompt", () => {
  const [, carol, team] = grantRows(APP);
  assert.ok(carol && team);

  it("warns an app admin who lowers their own grant, and names the level they keep", () => {
    const prompt = grantLevelChangePrompt(carol, MANAGER);
    assert.ok(prompt !== null);
    assert.match(prompt, /your own/i);
    assert.match(prompt, /Can use/);
    assert.match(prompt, /lose/i);
  });

  it("matches the email without regard to case", () => {
    const shouting = { ...MANAGER, email: "Carol@Example.com" };
    assert.notEqual(grantLevelChangePrompt(carol, shouting), null);
  });

  it("stays quiet for a platform admin, who manages every app without a grant", () => {
    const admin = { ...MANAGER, isPlatformAdmin: true };
    assert.equal(grantLevelChangePrompt(carol, admin), null);
  });

  it("stays quiet when the grant is somebody else's", () => {
    const dana = { ...MANAGER, email: "dana@example.com" };
    assert.equal(grantLevelChangePrompt(carol, dana), null);
    assert.equal(grantLevelChangePrompt(team, MANAGER), null);
  });

  it("stays quiet when the change raises a level, and when the actor is not known yet", () => {
    const carolAsUser = { ...carol, permission: "CAN_USE" as const };
    assert.equal(grantLevelChangePrompt(carolAsUser, MANAGER), null);
    assert.equal(grantLevelChangePrompt(carol, null), null);
  });

  it("is not about a group that happens to be named like the actor", () => {
    const lookalike = { ...carol, type: "group" as const };
    assert.equal(grantLevelChangePrompt(lookalike, MANAGER), null);
  });
});

describe("grantRemoveAvailability and grantRemovePrompt", () => {
  const [owner, carol] = grantRows(APP);
  assert.ok(owner && carol);

  it("is allowed for an admin on any grant but the owner's", () => {
    assert.deepEqual(grantRemoveAvailability(carol, MANAGER), {
      isAllowed: true,
    });
    const refusal = grantRemoveAvailability(owner, MANAGER);
    assert.ok(!refusal.isAllowed);
    assert.match(refusal.reason, /owner/i);
  });

  it("is refused to a visitor, and waits for an unknown actor", () => {
    assert.ok(!grantRemoveAvailability(carol, VISITOR).isAllowed);
    assert.ok(!grantRemoveAvailability(carol, null).isAllowed);
  });

  it("asks the question with the name and the level", () => {
    const prompt = grantRemovePrompt(carol, "dana@example.com");
    assert.match(prompt, /carol@example\.com/);
    assert.match(prompt, /Can manage/);
  });

  it("warns a person who removes their own access", () => {
    const prompt = grantRemovePrompt(carol, "carol@example.com");
    assert.match(prompt, /your own/i);
  });
});
