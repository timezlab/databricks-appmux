import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { ApiError, CreatePermissionRequest, RequestRow } from "@/lib/api";
import {
  EMPTY_REGISTER_FORM,
  MEMORY_BUDGET_MAX_MB,
  MEMORY_BUDGET_MIN_MB,
  REASON_MAX_LENGTH,
  submitRegisterForm,
  validateRegisterForm,
  withoutFieldError,
} from "@/lib/launcher/register-form";
import type {
  RegisterFormContext,
  RegisterFormValues,
} from "@/lib/launcher/register-form";
import { MAX_PENDING_REGISTER_REQUESTS } from "@/lib/launcher/requests";
import { readExample } from "../../../support/contract-examples";

const VALID: RegisterFormValues = {
  slug: "wiki",
  displayName: "Team wiki",
  sourcePath: "/Workspace/Shared/apps/wiki",
  memoryBudgetMb: "128",
  reason: "the team wiki, a small Flask app",
};
const NOTHING_KNOWN: RegisterFormContext = {
  knownSlugs: [],
  pendingRegisterSlugs: [],
};

function errorsOf(
  values: Partial<RegisterFormValues>,
  context: RegisterFormContext = NOTHING_KNOWN,
) {
  const result = validateRegisterForm({ ...VALID, ...values }, context);
  assert.equal(result.ok, false, JSON.stringify(values));
  return result.ok ? {} : result.errors;
}

describe("a valid form", () => {
  it("becomes exactly the body of the contract's register example", () => {
    const result = validateRegisterForm(VALID, NOTHING_KNOWN);
    assert.ok(result.ok);
    assert.deepEqual(result.request, readExample("CreatePermissionRequest"));
  });

  it("is a body the contract's own schema accepts", () => {
    const result = validateRegisterForm(VALID, NOTHING_KNOWN);
    assert.ok(result.ok);
    assert.ok(CreatePermissionRequest.safeParse(result.request).success);
  });

  it("trims what a paste drags along", () => {
    const result = validateRegisterForm(
      {
        slug: "  wiki ",
        displayName: " Team wiki\n",
        sourcePath: " /Workspace/Shared/apps/wiki ",
        memoryBudgetMb: " 128 ",
        reason: "  because \n",
      },
      NOTHING_KNOWN,
    );
    assert.ok(result.ok);
    assert.deepEqual(result.request, {
      kind: "register",
      app: "wiki",
      payload: {
        slug: "wiki",
        display_name: "Team wiki",
        source_path: "/Workspace/Shared/apps/wiki",
        memory_budget_mb: 128,
      },
      reason: "because",
    });
  });
});

describe("an empty form", () => {
  it("names every field, each in its own words", () => {
    const errors = errorsOf({
      ...EMPTY_REGISTER_FORM,
    });
    assert.deepEqual(Object.keys(errors).toSorted(), [
      "displayName",
      "memoryBudgetMb",
      "reason",
      "slug",
      "sourcePath",
    ]);
    assert.equal(new Set(Object.values(errors)).size, 5);
  });
});

describe("the slug (RegisterAppRequest.slug)", () => {
  it("accepts the edges of the pattern", () => {
    for (const slug of ["a", "0", "9lives", "my-app", "x".repeat(31)]) {
      assert.ok(
        validateRegisterForm({ ...VALID, slug }, NOTHING_KNOWN).ok,
        slug,
      );
    }
  });

  it("refuses what the pattern refuses, on the slug field only", () => {
    for (const slug of [
      "Sales",
      "-sales",
      "my_app",
      "my app",
      "x".repeat(32),
      "_appmux",
      "../x",
      "a/b",
    ]) {
      const errors = errorsOf({ slug });
      assert.deepEqual(Object.keys(errors), ["slug"], slug);
    }
  });

  it("refuses a slug an app the visitor can see already has", () => {
    const errors = errorsOf(
      { slug: "sales" },
      { knownSlugs: ["sales"], pendingRegisterSlugs: [] },
    );
    assert.match(errors.slug ?? "", /already/i);
  });

  it("refuses a slug the visitor already asked for and that is still pending", () => {
    const errors = errorsOf(
      { slug: "wiki" },
      { knownSlugs: [], pendingRegisterSlugs: ["wiki"] },
    );
    assert.match(errors.slug ?? "", /pending/i);
  });
});

describe("the source folder", () => {
  it("must start with /Workspace/", () => {
    assert.deepEqual(Object.keys(errorsOf({ sourcePath: "/tmp/wiki" })), [
      "sourcePath",
    ]);
    assert.deepEqual(Object.keys(errorsOf({ sourcePath: "Workspace/x" })), [
      "sourcePath",
    ]);
    assert.ok(
      validateRegisterForm(
        { ...VALID, sourcePath: "/Workspace/x" },
        NOTHING_KNOWN,
      ).ok,
    );
  });
});

describe("the display name", () => {
  it("must not be blank", () => {
    assert.deepEqual(Object.keys(errorsOf({ displayName: "   " })), [
      "displayName",
    ]);
  });
});

describe("the memory budget", () => {
  it("is 64 to 16384 MB, edges included", () => {
    assert.equal(MEMORY_BUDGET_MIN_MB, 64);
    assert.equal(MEMORY_BUDGET_MAX_MB, 16384);
    for (const memoryBudgetMb of ["64", "16384", "1024"]) {
      assert.ok(
        validateRegisterForm({ ...VALID, memoryBudgetMb }, NOTHING_KNOWN).ok,
        memoryBudgetMb,
      );
    }
    for (const memoryBudgetMb of ["63", "16385", "0"]) {
      const errors = errorsOf({ memoryBudgetMb });
      assert.deepEqual(Object.keys(errors), ["memoryBudgetMb"], memoryBudgetMb);
      assert.match(errors.memoryBudgetMb ?? "", /64/);
      assert.match(errors.memoryBudgetMb ?? "", /16,?384/);
    }
  });

  it("must be a whole number", () => {
    for (const memoryBudgetMb of [
      "abc",
      "12.5",
      "1e3",
      "-64",
      "0x40",
      "64MB",
    ]) {
      const errors = errorsOf({ memoryBudgetMb });
      assert.deepEqual(Object.keys(errors), ["memoryBudgetMb"], memoryBudgetMb);
      assert.match(errors.memoryBudgetMb ?? "", /whole number/i);
    }
  });

  it("is required", () => {
    assert.match(
      errorsOf({ memoryBudgetMb: " " }).memoryBudgetMb ?? "",
      /enter/i,
    );
  });
});

describe("the reason", () => {
  it("is required and not blank", () => {
    assert.deepEqual(Object.keys(errorsOf({ reason: " \n " })), ["reason"]);
  });

  it("takes up to 500 characters and no more", () => {
    assert.equal(REASON_MAX_LENGTH, 500);
    assert.ok(
      validateRegisterForm({ ...VALID, reason: "x".repeat(500) }, NOTHING_KNOWN)
        .ok,
    );
    const errors = errorsOf({ reason: "x".repeat(501) });
    assert.deepEqual(Object.keys(errors), ["reason"]);
    assert.match(errors.reason ?? "", /500/);
  });
});

describe("the cap of pending requests (FR-041)", () => {
  it("refuses a fourth pending register request, before sending", () => {
    const slugs = Array.from(
      { length: MAX_PENDING_REGISTER_REQUESTS },
      (_, index) => `app-${index + 1}`,
    );
    const errors = errorsOf(
      {},
      { knownSlugs: [], pendingRegisterSlugs: slugs },
    );
    assert.match(
      errors._form ?? "",
      new RegExp(String(MAX_PENDING_REGISTER_REQUESTS)),
    );
  });

  it("allows the third", () => {
    const result = validateRegisterForm(VALID, {
      knownSlugs: [],
      pendingRegisterSlugs: ["a", "b"],
    });
    assert.ok(result.ok);
  });

  it("leaves it to the server while the list of requests is not loaded", () => {
    const result = validateRegisterForm(VALID, {
      knownSlugs: [],
      pendingRegisterSlugs: null,
    });
    assert.ok(result.ok);
  });
});

describe("submitRegisterForm", () => {
  const row = RequestRow.parse(readExample("RequestRow"));

  it("sends nothing while the form is invalid", async () => {
    let calls = 0;
    const result = await submitRegisterForm(
      { ...VALID, slug: "Bad Slug" },
      NOTHING_KNOWN,
      () => {
        calls += 1;
        return Promise.resolve(row);
      },
    );
    assert.equal(result.status, "invalid");
    assert.equal(calls, 0);
  });

  it("sends the validated body once and returns the new request", async () => {
    const sent: CreatePermissionRequest[] = [];
    const result = await submitRegisterForm(VALID, NOTHING_KNOWN, (body) => {
      sent.push(body);
      return Promise.resolve(row);
    });
    assert.deepEqual(result, { status: "sent", request: row });
    assert.deepEqual(sent, [readExample("CreatePermissionRequest")]);
  });

  it("hands the server's refusal back unchanged, for the page to show", async () => {
    const refusal = new ApiError(
      400,
      "slug_taken",
      "slug 'wiki' is already taken",
    );
    const result = await submitRegisterForm(VALID, NOTHING_KNOWN, () =>
      Promise.reject(refusal),
    );
    assert.deepEqual(result, { status: "refused", error: refusal });
  });
});

describe("withoutFieldError", () => {
  it("drops the error of the field the person just edited and keeps the others", () => {
    const errors = {
      slug: "Enter a slug.",
      reason: "Say why.",
      _form: "You already have 3 pending requests.",
    };
    assert.deepEqual(withoutFieldError(errors, "slug"), {
      reason: "Say why.",
      _form: "You already have 3 pending requests.",
    });
  });

  it("leaves the errors as they are when the field had none, and does not change its input", () => {
    const errors = { reason: "Say why." };
    assert.deepEqual(withoutFieldError(errors, "slug"), errors);
    withoutFieldError(errors, "reason");
    assert.deepEqual(errors, { reason: "Say why." });
  });
});
