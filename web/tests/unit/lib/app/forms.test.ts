import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { CreatePermissionRequest, UpdateReleaseRequest } from "@/lib/api";
import {
  FORM_ERROR_KEY,
  errorsFromZod,
  friendlyIssueMessage,
  withoutError,
} from "@/lib/app/forms";

function failureOf(schema: {
  safeParse: (input: unknown) => {
    success: boolean;
    error?: Parameters<typeof errorsFromZod>[0];
  };
}) {
  return (input: unknown) => {
    const parsed = schema.safeParse(input);
    assert.ok(!parsed.success && parsed.error, "the input must be refused");
    return parsed.error;
  };
}

describe("friendlyIssueMessage", () => {
  const failTags = failureOf(UpdateReleaseRequest);

  it("reads a too-long string as a character limit", () => {
    const error = failTags({ name: "x".repeat(81), tags: [] });
    const [issue] = error.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "At most 80 characters");
  });

  it("reads a too-long array as an item limit", () => {
    const error = failTags({
      name: null,
      tags: Array.from({ length: 11 }, (_, index) => `t${index}`),
    });
    const [issue] = error.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "At most 10 items");
  });

  it("reads a number outside its range with the bound", () => {
    const failRequest = failureOf(CreatePermissionRequest);
    const small = failRequest({
      kind: "settings",
      app: "sales",
      payload: { memory_budget_mb: 8 },
      reason: "more room",
    });
    const [issue] = small.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "At least 64");
  });

  it("keeps the contract's own message for a custom rule", () => {
    const error = failTags({ name: null, tags: ["Stable"] });
    const [issue] = error.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "must be lower case");
  });
});

describe("errorsFromZod", () => {
  const failRequest = failureOf(CreatePermissionRequest);

  it("keys each message by the field name, one message per field", () => {
    const error = failRequest({
      kind: "settings",
      app: "sales",
      payload: { slug: "Not A Slug", memory_budget_mb: 1 },
      reason: "  ",
    });
    assert.deepEqual(errorsFromZod(error, { dropPathPrefix: "payload" }), {
      slug: "lower-case letters, digits and -, at most 31 characters",
      memory_budget_mb: "At least 64",
      reason: "must not be blank",
    });
  });

  it("puts a rule that spans fields under the form key", () => {
    const error = failRequest({
      kind: "settings",
      app: "sales",
      payload: {},
      reason: "why",
    });
    assert.deepEqual(errorsFromZod(error, { dropPathPrefix: "payload" }), {
      [FORM_ERROR_KEY]: "ask for at least one field",
    });
  });

  it("can fold array positions into the field (tags.3 is tags)", () => {
    const error = UpdateReleaseRequest.safeParse({
      name: null,
      tags: ["ok", "Bad"],
    }).error;
    assert.ok(error);
    assert.deepEqual(errorsFromZod(error, { dropIndexes: true }), {
      tags: "must be lower case",
    });
    assert.deepEqual(Object.keys(errorsFromZod(error)), ["tags.1"]);
  });
});

describe("withoutError", () => {
  it("drops the field the person is editing and the form-wide message", () => {
    assert.deepEqual(
      withoutError(
        { slug: "bad", reason: "empty", [FORM_ERROR_KEY]: "all" },
        "slug",
      ),
      { reason: "empty" },
    );
  });

  it("returns the same object when there is nothing to drop", () => {
    const errors = { reason: "empty" };
    assert.equal(withoutError(errors, "slug"), errors);
  });
});
