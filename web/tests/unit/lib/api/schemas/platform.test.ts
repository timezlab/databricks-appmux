/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { readExampleObject } from "../../../../support/contract-examples";
import { accepts, rejects } from "../../../../support/schema-assertions";

describe("platform admins", () => {
  const user = { type: "user", name: "dana@example.com" };

  it("the list names users and groups only, with at least one user", () => {
    accepts(api.PutAdminsRequest, { admins: [user], reason: "r" });
    accepts(api.PutAdminsRequest, {
      admins: [{ type: "group", name: "data-platform" }, user],
      reason: "r",
    });
    rejects(api.PutAdminsRequest, { admins: [], reason: "r" }, "empty");
    rejects(
      api.PutAdminsRequest,
      { admins: [{ type: "group", name: "data-platform" }], reason: "r" },
      "groups only",
    );
    rejects(
      api.PutAdminsRequest,
      { admins: [user, { type: "everyone", name: "*" }], reason: "r" },
      "everyone",
    );
    rejects(
      api.PutAdminsRequest,
      { admins: [{ type: "user", name: "Dana@example.com" }], reason: "r" },
      "an upper-case email",
    );
    rejects(
      api.PutAdminsRequest,
      { admins: [{ ...user, is_admin: true }], reason: "r" },
      "an unlisted field in an entry",
    );
  });
});

describe("platform admins: duplicates", () => {
  const user = { type: "user", name: "dana@example.com" };

  it("refuses a duplicate of the same type after lower-casing the name", () => {
    rejects(
      api.PutAdminsRequest,
      { admins: [user, user], reason: "r" },
      "the same user twice",
    );
    rejects(
      api.PutAdminsRequest,
      {
        admins: [
          user,
          { type: "group", name: "Data-Platform" },
          { type: "group", name: "data-platform" },
        ],
        reason: "r",
      },
      "a group twice, differing in case",
    );
  });

  it("allows the same name as a user and as a group", () => {
    accepts(api.PutAdminsRequest, {
      admins: [user, { type: "group", name: "dana@example.com" }],
      reason: "r",
    });
  });
});

describe("settings", () => {
  it("PUT /platform/settings/{key} takes a JSON value and nothing else", () => {
    accepts(api.PutSettingRequest, { value: 10 });
    accepts(api.PutSettingRequest, { value: ["/Workspace/wheels"] });
    accepts(api.PutSettingRequest, { value: null });
    rejects(api.PutSettingRequest, {});
    rejects(api.PutSettingRequest, { value: 1, key: "x" });
  });

  it("the setting keys are the keys of PlatformView.settings", () => {
    const settings = readExampleObject("PlatformView").settings;
    assert.ok(typeof settings === "object" && settings !== null);
    assert.deepEqual(
      [...api.SettingKey.options].sort(),
      Object.keys(settings).sort(),
    );
  });
});

describe("AuditQuery (GET /audit?app=&from=&to=&limit=)", () => {
  it("takes every parameter as optional, and at most 1000 rows", () => {
    accepts(api.AuditQuery, {});
    accepts(api.AuditQuery, {
      app: "sales",
      from: "2026-10-30T08:00:00Z",
      to: "2026-10-30T09:00:00.000Z",
      limit: 1,
    });
    accepts(api.AuditQuery, { limit: 1000 });
    for (const limit of [0, -1, 1001, 1.5, Number.NaN, "50"]) {
      rejects(api.AuditQuery, { limit }, `limit ${String(limit)}`);
    }
  });

  it("wants ISO 8601 UTC times and a slug", () => {
    for (const bad of [
      "yesterday",
      "2026-10-30",
      "2026-10-30T08:00:00+02:00",
    ]) {
      rejects(api.AuditQuery, { from: bad }, bad);
      rejects(api.AuditQuery, { to: bad }, bad);
    }
    for (const bad of ["Sales", "a/b", "", "_platform"]) {
      rejects(api.AuditQuery, { app: bad }, bad);
    }
  });

  it("refuses a parameter the route does not have", () => {
    rejects(api.AuditQuery, { aps: "sales" });
  });
});
