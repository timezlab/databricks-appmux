import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { Principal } from "@/lib/api/schemas";
import { adminRows, parseAdminChange } from "@/lib/platform/admins";

const DANA: Principal = { type: "user", name: "dana@example.com" };
const ERIN: Principal = { type: "user", name: "erin@example.com" };
const TEAM: Principal = { type: "group", name: "data-platform" };

const ME = { email: "dana@example.com", groups: [] as string[] };

describe("adminRows", () => {
  it("lists every admin, marks the signed-in person, and keeps the server's order", () => {
    const rows = adminRows([DANA, TEAM, ERIN], ME);
    assert.deepEqual(
      rows.map((row) => [row.name, row.typeLabel, row.isYou]),
      [
        ["dana@example.com", "User", true],
        ["data-platform", "Group", false],
        ["erin@example.com", "User", false],
      ],
    );
  });

  it("blocks removing the only user, and says why", () => {
    const rows = adminRows([DANA, TEAM], ME);
    assert.equal(rows[0]?.canRemove, false);
    assert.match(rows[0]?.blockedReason ?? "", /at least one user/i);
    assert.equal(rows[1]?.canRemove, true);
  });

  it("allows removing a user while another user stays", () => {
    const rows = adminRows([DANA, ERIN], ME);
    assert.ok(rows.every((row) => row.canRemove));
  });

  it("warns when a removal would take the signed-in person's own access", () => {
    const rows = adminRows([DANA, ERIN], ME);
    assert.equal(rows[0]?.losesYourAccess, true);
    assert.equal(rows[1]?.losesYourAccess, false);
  });

  it("does not warn while a group the person belongs to still holds access", () => {
    const rows = adminRows([DANA, ERIN, TEAM], {
      email: "dana@example.com",
      groups: ["data-platform"],
    });
    assert.equal(rows[0]?.losesYourAccess, false);
    assert.equal(rows[2]?.losesYourAccess, false);
  });

  it("warns when removing the group is what takes the access", () => {
    const rows = adminRows([ERIN, TEAM], {
      email: "dana@example.com",
      groups: ["data-platform"],
    });
    assert.equal(rows[1]?.losesYourAccess, true);
  });
});

describe("parseAdminChange: add", () => {
  const add = (type: "user" | "group", name: string) =>
    ({ kind: "add", type, name }) as const;

  it("appends the new admin to the whole list, as the PUT replaces it", () => {
    assert.deepEqual(
      parseAdminChange(
        [DANA],
        add("user", " erin@example.com "),
        "new on call",
      ),
      {
        ok: true,
        value: { admins: [DANA, ERIN], reason: "new on call" },
      },
    );
    assert.deepEqual(
      parseAdminChange([DANA], add("group", "data-platform"), "team"),
      { ok: true, value: { admins: [DANA, TEAM], reason: "team" } },
    );
  });

  it("refuses a name that is blank, a user that is not an email, and an upper-case one", () => {
    const blank = parseAdminChange([DANA], add("user", " "), "why");
    assert.equal(blank.ok, false);
    if (!blank.ok) assert.ok(blank.errors.name);
    const notEmail = parseAdminChange([DANA], add("user", "erin"), "why");
    assert.equal(notEmail.ok, false);
    if (!notEmail.ok) assert.ok(notEmail.errors.name);
    const upper = parseAdminChange(
      [DANA],
      add("user", "Erin@Example.com"),
      "why",
    );
    assert.equal(upper.ok, false);
    if (!upper.ok) assert.equal(upper.errors.name, "must be lower case");
    const blankGroup = parseAdminChange([DANA], add("group", " "), "why");
    assert.equal(blankGroup.ok, false);
    if (!blankGroup.ok) assert.ok(blankGroup.errors.name);
  });

  it("refuses an admin who is already listed", () => {
    assert.deepEqual(
      parseAdminChange([DANA], add("user", "dana@example.com"), "why"),
      { ok: false, errors: { name: "is already a platform admin" } },
    );
  });

  it("treats a group that differs only in case as already listed, on the name field", () => {
    const group = { type: "group" as const, name: "data-platform" };
    assert.deepEqual(
      parseAdminChange([DANA, group], add("group", "Data-Platform"), "why"),
      { ok: false, errors: { name: "is already a platform admin" } },
    );
  });

  it("still reports the reason next to an already listed admin", () => {
    assert.deepEqual(
      parseAdminChange([DANA], add("user", "dana@example.com"), " "),
      {
        ok: false,
        errors: {
          name: "is already a platform admin",
          reason: "must not be blank",
        },
      },
    );
  });

  it("requires a reason", () => {
    assert.deepEqual(
      parseAdminChange([DANA], add("user", "erin@example.com"), "  "),
      { ok: false, errors: { reason: "must not be blank" } },
    );
  });

  it("reports the name and the reason together", () => {
    const result = parseAdminChange([DANA], add("user", "erin"), "");
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.deepEqual(Object.keys(result.errors).toSorted(), [
        "name",
        "reason",
      ]);
    }
  });
});

describe("parseAdminChange: remove", () => {
  const remove = (entry: Principal) =>
    ({ kind: "remove", type: entry.type, name: entry.name }) as const;

  it("sends the list without the removed admin", () => {
    assert.deepEqual(
      parseAdminChange([DANA, ERIN, TEAM], remove(ERIN), "left the team"),
      {
        ok: true,
        value: { admins: [DANA, TEAM], reason: "left the team" },
      },
    );
  });

  it("refuses to remove the last user, even with a group left", () => {
    const result = parseAdminChange([DANA, TEAM], remove(DANA), "why");
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.errors._form ?? "", /at least one user/i);
    }
  });

  it("says the list changed when the admin is no longer in it", () => {
    const result = parseAdminChange([DANA], remove(ERIN), "why");
    assert.equal(result.ok, false);
    if (!result.ok)
      assert.match(result.errors._form ?? "", /no longer in the list/i);
  });

  it("requires a reason", () => {
    const result = parseAdminChange([DANA, ERIN], remove(ERIN), "");
    assert.deepEqual(result, {
      ok: false,
      errors: { reason: "must not be blank" },
    });
  });
});

describe("parseAdminChange: a list the server sent that this page cannot send back", () => {
  const everyone: Principal = { type: "everyone", name: "*" };

  it("names the entry instead of failing silently", () => {
    const result = parseAdminChange(
      [DANA, everyone],
      { kind: "add", type: "user", name: "erin@example.com" },
      "why",
    );
    assert.equal(result.ok, false);
    if (!result.ok) assert.match(result.errors._form ?? "", /everyone/);
  });

  it("lets the entry itself be removed", () => {
    assert.deepEqual(
      parseAdminChange(
        [DANA, everyone],
        { kind: "remove", type: "everyone", name: "*" },
        "not a valid admin",
      ),
      { ok: true, value: { admins: [DANA], reason: "not a valid admin" } },
    );
  });
});
