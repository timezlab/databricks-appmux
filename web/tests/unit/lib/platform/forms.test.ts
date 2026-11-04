import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { z } from "zod";

import {
  DelegationReasonRequest,
  PutEgressRequest,
  RegisterAppRequest,
} from "@/lib/api/schemas";
import { formErrors, parseWholeNumber } from "@/lib/platform/forms";

function issuesOf(result: z.ZodSafeParseResult<unknown>) {
  assert.equal(result.success, false);
  return result.success ? [] : result.error.issues;
}

describe("parseWholeNumber", () => {
  it("reads digits, with spaces around them ignored", () => {
    assert.equal(parseWholeNumber("512"), 512);
    assert.equal(parseWholeNumber("  512 "), 512);
    assert.equal(parseWholeNumber("0"), 0);
  });

  it("refuses everything that is not plain digits, so a typo never becomes a different number", () => {
    for (const text of ["", "  ", "5.5", "-3", "+3", "1e3", "0x10", "1,024"]) {
      assert.equal(parseWholeNumber(text), null, JSON.stringify(text));
    }
  });

  it("refuses a number too long to be exact", () => {
    assert.equal(parseWholeNumber("9".repeat(16)), null);
  });
});

describe("formErrors", () => {
  const valid = {
    slug: "sales",
    display_name: "Sales",
    source_path: "/Workspace/Shared/apps/sales",
    memory_budget_mb: 512,
    owner: "alice@example.com",
  };

  it("keeps the contract's own wording for a rule written in the schema", () => {
    const errors = formErrors(
      issuesOf(
        RegisterAppRequest.safeParse({ ...valid, source_path: "/tmp/x" }),
      ),
    );
    assert.deepEqual(errors, { source_path: "must start with /Workspace/" });
  });

  it("replaces zod's English for a number out of range with the bounds", () => {
    assert.deepEqual(
      formErrors(
        issuesOf(
          RegisterAppRequest.safeParse({ ...valid, memory_budget_mb: 10 }),
        ),
      ),
      { memory_budget_mb: "must be at least 256" },
    );
    assert.deepEqual(
      formErrors(
        issuesOf(
          RegisterAppRequest.safeParse({ ...valid, memory_budget_mb: 99999 }),
        ),
      ),
      { memory_budget_mb: "must be at most 16,384" },
    );
  });

  it("states a text limit in characters", () => {
    assert.deepEqual(
      formErrors(
        issuesOf(
          DelegationReasonRequest.safeParse({
            version: 1,
            reason: "x".repeat(501),
          }),
        ),
      ),
      { reason: "must be at most 500 characters" },
    );
  });

  it("explains a CIDR that matches none of the three forms", () => {
    const errors = formErrors(
      issuesOf(
        PutEgressRequest.safeParse({
          rules: [{ action: "deny", cidr: "ten", port: "any", comment: "" }],
        }),
      ),
    );
    assert.match(errors["rules.0.cidr"] ?? "", /CIDR such as 10\.20\.0\.0\/16/);
    assert.match(errors["rules.0.cidr"] ?? "", /@container_ip\/32/);
  });

  it("keeps the placeholder in an overlap refusal", () => {
    const errors = formErrors(
      issuesOf(
        PutEgressRequest.safeParse({
          rules: [
            {
              action: "allow",
              cidr: "@container_ip/32",
              port: "443",
              comment: "",
            },
          ],
        }),
      ),
    );
    assert.equal(
      errors["rules.0.cidr"],
      "an allow cannot overlap @container_ip",
    );
  });

  it("files a whole-form rule under _form", () => {
    assert.deepEqual(
      formErrors([{ code: "custom", path: [], message: "nothing to change" }]),
      { _form: "nothing to change" },
    );
  });

  it("says a missing value is required", () => {
    assert.deepEqual(
      formErrors([
        {
          code: "invalid_type",
          path: ["owner"],
          message: "Invalid input: expected string, received undefined",
          expected: "string",
          input: undefined,
        },
      ]),
      { owner: "is required" },
    );
  });
});
