/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { readExampleObject } from "../../../../support/contract-examples";
import {
  accepts,
  rejects,
  variant,
} from "../../../../support/schema-assertions";

describe("Time", () => {
  const row = readExampleObject("AuditRow");

  it("is an ISO 8601 UTC string ending in Z", () => {
    accepts(api.AuditRow, variant(row, { ts: "2026-10-30T09:14:00Z" }));
    for (const bad of [
      "2026-10-30",
      "2026-10-30T09:14:00",
      "2026-10-30T09:14:00+02:00",
      "2026-10-30 09:14:00Z",
      "2026-13-30T09:14:00Z",
      "yesterday",
      "",
      1730279640,
    ]) {
      rejects(api.AuditRow, variant(row, { ts: bad }), String(bad));
    }
  });

  it("is checked in the nullable fields too", () => {
    const release = readExampleObject("ReleaseSummary");
    accepts(api.ReleaseSummary, variant(release, { locked_at: null }));
    rejects(api.ReleaseSummary, variant(release, { locked_at: "soon" }));
  });
});

describe("ResourceKey", () => {
  it("is 1 to 63 of lower-case letters, digits, '.', '_' and '-', starting with a letter or digit", () => {
    for (const good of ["a", "0", "a.b_c-d", "a".repeat(63)]) {
      accepts(api.ResourceKey, good, good);
    }
    for (const bad of [
      "",
      "A",
      "a b",
      "a/b",
      "a@b",
      "a".repeat(64),
      ".a",
      "_a",
      "-a",
    ]) {
      rejects(api.ResourceKey, bad, bad);
    }
  });
});

describe("email addresses", () => {
  const owner = (name: string) =>
    variant(readExampleObject("SetOwnerRequest"), { owner: name });

  it("follow the backend's shape: name@domain with a dot, no space, comma or semicolon", () => {
    for (const good of [
      "alice@example.com",
      "a.b+tag@sub.example.co",
      "o'brien@example.com",
      "a#b@example.com",
      "a@b.c",
    ]) {
      accepts(api.SetOwnerRequest, owner(good), good);
    }
    for (const bad of [
      "",
      "alice",
      "alice@example",
      "alice@@example.com",
      "a b@example.com",
      "alice@example.com ",
      " alice@example.com",
      "a@b.com,c@d.com",
      "a@b.com;c@d.com",
      "alice@.com",
      "alice@example..com",
      "Alice@example.com",
    ]) {
      rejects(api.SetOwnerRequest, owner(bad), JSON.stringify(bad));
    }
  });
});
