import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { FORM_ERROR_KEY } from "@/lib/app/forms";
import {
  EMPTY_GRANT_FORM,
  buildGrantRequest,
  choosePrincipal,
  grantLevelChoices,
  inferPrincipalType,
  isSearchablePrincipalText,
  principalRows,
  principalSuggestionsNote,
  typedPrincipal,
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

const SP_ID = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab";

describe("inferPrincipalType", () => {
  it("reads an @ as a user's e-mail", () => {
    assert.equal(inferPrincipalType("erin@example.com"), "user");
    assert.equal(inferPrincipalType("erin@"), "user");
  });

  it("reads a UUID, in either case, as a service principal's application id", () => {
    assert.equal(inferPrincipalType(SP_ID), "service_principal");
    assert.equal(inferPrincipalType(SP_ID.toUpperCase()), "service_principal");
  });

  it("reads everyone and * as everyone, in any case and with spaces around", () => {
    assert.equal(inferPrincipalType("everyone"), "everyone");
    assert.equal(inferPrincipalType(" Everyone "), "everyone");
    assert.equal(inferPrincipalType("*"), "everyone");
  });

  it("reads anything else as a group name, and nothing as nothing", () => {
    assert.equal(inferPrincipalType("data team"), "group");
    assert.equal(inferPrincipalType("4f3c1b2a"), "group");
    assert.equal(inferPrincipalType("   "), null);
  });
});

describe("typedPrincipal", () => {
  it("normalises the name the grant will carry", () => {
    assert.deepEqual(typedPrincipal(" Erin@Example.com "), {
      type: "user",
      name: "erin@example.com",
      label: "erin@example.com",
      identifier: null,
    });
    assert.deepEqual(typedPrincipal(SP_ID.toUpperCase()), {
      type: "service_principal",
      name: SP_ID,
      label: SP_ID,
      identifier: null,
    });
    assert.deepEqual(typedPrincipal("*"), {
      type: "everyone",
      name: "*",
      label: "Everyone",
      identifier: null,
    });
    assert.equal(typedPrincipal(" Data Team ")?.name, "Data Team");
    assert.equal(typedPrincipal(""), null);
  });
});

describe("principalRows", () => {
  const LIST = api.PrincipalList.parse(readExample("PrincipalList"));

  it("keeps the server's order and shows the display name with the identifier beside it", () => {
    const rows = principalRows(LIST.items, "");
    assert.deepEqual(
      rows.map((row) => [
        row.choice.type,
        row.choice.label,
        row.choice.identifier,
      ]),
      [
        ["everyone", "Everyone", null],
        ["group", "sales-team", null],
        ["user", "Erin Ng", "erin@example.com"],
        ["service_principal", "sales-agent", SP_ID],
      ],
    );
    assert.ok(rows.every((row) => !row.isTyped));
  });

  it("offers what was typed last, as a Use row with the inferred type", () => {
    const rows = principalRows([], "data-team");
    assert.deepEqual(
      rows.map((row) => [row.isTyped, row.choice.type, row.choice.name]),
      [[true, "group", "data-team"]],
    );
    assert.equal(rows[0]?.text, "Use “data-team” as a group");
  });

  it("does not offer the typed text again when a suggestion is the same principal", () => {
    const rows = principalRows(LIST.items, "Erin@example.com");
    assert.equal(
      rows.some((row) => row.isTyped),
      false,
    );
    assert.equal(
      principalRows(LIST.items, "everyone").some((row) => row.isTyped),
      false,
    );
  });

  it("gives every row a distinct id", () => {
    const rows = principalRows(LIST.items, "new-group");
    assert.equal(new Set(rows.map((row) => row.id)).size, rows.length);
  });
});

describe("principalSuggestionsNote", () => {
  it("says nothing when every directory was read", () => {
    assert.equal(
      principalSuggestionsNote({
        users: "caller_token",
        groups: "platform_token",
        service_principals: "not_read",
      }),
      null,
    );
  });

  it("names the directories that could not be read, and says typing still works", () => {
    const note = principalSuggestionsNote({
      users: "unavailable",
      groups: "caller_token",
      service_principals: "unavailable",
    });
    assert.match(note ?? "", /users and service principals/);
    assert.match(note ?? "", /type/i);
  });

  it("says suggestions are unavailable when the call failed", () => {
    assert.match(principalSuggestionsNote(null) ?? "", /unavailable/i);
  });
});

describe("isSearchablePrincipalText", () => {
  it("lets through what the server can search, and holds back the rest", () => {
    assert.equal(isSearchablePrincipalText("erin@example.com"), true);
    assert.equal(isSearchablePrincipalText(""), true);
    assert.equal(isSearchablePrincipalText("*"), false);
    assert.equal(isSearchablePrincipalText("x".repeat(65)), false);
  });
});

describe("grantLevelChoices", () => {
  it("offers both levels for a user or a group", () => {
    for (const type of ["user", "group"] as const) {
      assert.deepEqual(
        grantLevelChoices(type).map((choice) => [
          choice.value,
          choice.isAvailable,
        ]),
        [
          ["CAN_USE", true],
          ["CAN_MANAGE", true],
        ],
      );
    }
  });

  it("disables Can manage for everyone and a service principal, with the reason", () => {
    for (const type of ["everyone", "service_principal"] as const) {
      const manage = grantLevelChoices(type).find(
        (choice) => choice.value === "CAN_MANAGE",
      );
      assert.ok(manage);
      assert.equal(manage.isAvailable, false);
      assert.match(manage.reason ?? "", /only use/i);
    }
  });
});

describe("choosePrincipal", () => {
  it("drops Can manage when the new principal cannot hold it", () => {
    const manage = { ...EMPTY_GRANT_FORM, permission: "CAN_MANAGE" as const };
    assert.equal(
      choosePrincipal(manage, { type: "everyone", name: "*" }).permission,
      "CAN_USE",
    );
    assert.equal(
      choosePrincipal(manage, { type: "service_principal", name: SP_ID })
        .permission,
      "CAN_USE",
    );
    assert.equal(
      choosePrincipal(manage, { type: "group", name: "sales-team" }).permission,
      "CAN_MANAGE",
    );
    assert.equal(choosePrincipal(manage, null).principal, null);
  });
});

describe("buildGrantRequest", () => {
  function build(
    type: api.GrantType,
    name: string,
    permission: api.AppPermission,
  ) {
    return buildGrantRequest(APP, { principal: { type, name }, permission });
  }

  it("builds a user grant with the email trimmed and lower-cased", () => {
    const result = build("user", "  Erin@Example.com ", "CAN_USE");
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "user",
      name: "erin@example.com",
      permission: "CAN_USE",
    });
  });

  it("builds a group grant with the name as typed, trimmed", () => {
    const result = build("group", " Data Platform ", "CAN_MANAGE");
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "group",
      name: "Data Platform",
      permission: "CAN_MANAGE",
    });
  });

  it("builds everyone as * with CAN_USE", () => {
    const result = build("everyone", "*", "CAN_USE");
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
  });

  it("builds a service principal grant from its application id, lower-cased (ADR 0020)", () => {
    const result = build("service_principal", SP_ID.toUpperCase(), "CAN_USE");
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      type: "service_principal",
      name: SP_ID,
      permission: "CAN_USE",
    });
  });

  it("refuses Can manage for everyone and for a service principal", () => {
    for (const [type, name] of [
      ["everyone", "*"],
      ["service_principal", SP_ID],
    ] as const) {
      const result = build(type, name, "CAN_MANAGE");
      assert.ok(!result.ok, type);
      assert.match(result.errors.permission ?? "", /only use/i);
    }
  });

  it("refuses a service principal named by anything but an application id", () => {
    const result = build("service_principal", "sales-agent", "CAN_USE");
    assert.ok(!result.ok);
    assert.match(result.errors.principal ?? "", /application id/i);
  });

  it("refuses an e-mail that is not complete, and a blank group", () => {
    const badUser = build("user", "carol@", "CAN_USE");
    assert.ok(!badUser.ok);
    assert.match(badUser.errors.principal ?? "", /e-mail/i);

    const blankGroup = build("group", "   ", "CAN_USE");
    assert.ok(!blankGroup.ok);
    assert.match(blankGroup.errors.principal ?? "", /group/i);
  });

  it("asks for someone when nobody is chosen", () => {
    const result = buildGrantRequest(APP, EMPTY_GRANT_FORM);
    assert.ok(!result.ok);
    assert.match(result.errors.principal ?? "", /choose/i);
    assert.equal(result.errors[FORM_ERROR_KEY], undefined);
  });

  it("refuses to give the owner a lower level, as the server does", () => {
    const result = build("user", "Alice@example.com", "CAN_USE");
    assert.ok(!result.ok);
    assert.match(result.errors.principal ?? "", /owner/i);
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
      label: "Make Can manage",
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

  it("offers nothing for a service principal, which is CAN_USE only", () => {
    assert.equal(
      grantLevelChange({
        id: "service_principal:4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab",
        type: "service_principal",
        name: "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab",
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
