import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { RegisterAppRequest } from "@/lib/api";
import {
  FORM_ERROR_KEY,
  fieldErrorsFromIssues,
} from "@/lib/forms/field-errors";

describe("fieldErrorsFromIssues", () => {
  it("keys each message by its field name", () => {
    assert.deepEqual(
      fieldErrorsFromIssues([
        { path: ["slug"], message: "too long" },
        { path: ["memory_budget_mb"], message: "too small" },
      ]),
      { slug: "too long", memory_budget_mb: "too small" },
    );
  });

  it("joins a nested path with dots, an index included", () => {
    assert.deepEqual(
      fieldErrorsFromIssues([
        { path: ["rules", 2, "cidr"], message: "not a CIDR" },
      ]),
      { "rules.2.cidr": "not a CIDR" },
    );
  });

  it("keeps the first message of a field: one error per field, the one to fix first", () => {
    assert.deepEqual(
      fieldErrorsFromIssues([
        { path: ["slug"], message: "first" },
        { path: ["slug"], message: "second" },
      ]),
      { slug: "first" },
    );
  });

  it("files an error about the whole form under FORM_ERROR_KEY", () => {
    assert.deepEqual(
      fieldErrorsFromIssues([{ path: [], message: "nothing to change" }]),
      { [FORM_ERROR_KEY]: "nothing to change" },
    );
  });

  it("is empty when there is nothing wrong", () => {
    assert.deepEqual(fieldErrorsFromIssues([]), {});
  });
});

describe("fieldErrorsFromIssues with the contract's request schemas", () => {
  const valid = {
    slug: "sales",
    display_name: "Sales",
    source_path: "/Workspace/Users/user@example.com/sales",
    memory_budget_mb: 512,
    owner: "user@example.com",
  };

  it("names the field the contract's rule refuses, with the rule as the message", () => {
    const parsed = RegisterAppRequest.safeParse({
      ...valid,
      slug: "Not A Slug",
    });
    assert.equal(parsed.success, false);
    if (parsed.success) return;
    const errors = fieldErrorsFromIssues(parsed.error.issues);
    assert.deepEqual(Object.keys(errors), ["slug"]);
    assert.match(errors.slug ?? "", /lower-case letters, digits and -/);
  });

  it("reports every wrong field at once", () => {
    const parsed = RegisterAppRequest.safeParse({
      ...valid,
      source_path: "/tmp/x",
      memory_budget_mb: 1,
    });
    assert.equal(parsed.success, false);
    if (parsed.success) return;
    assert.deepEqual(
      Object.keys(fieldErrorsFromIssues(parsed.error.issues)).toSorted(),
      ["memory_budget_mb", "source_path"],
    );
  });

  it("files an unknown key under the form, since it belongs to no field the person can see", () => {
    const parsed = RegisterAppRequest.safeParse({ ...valid, is_admin: true });
    assert.equal(parsed.success, false);
    if (parsed.success) return;
    assert.deepEqual(Object.keys(fieldErrorsFromIssues(parsed.error.issues)), [
      FORM_ERROR_KEY,
    ]);
  });
});
