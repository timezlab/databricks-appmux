/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  readExampleObject,
  without,
} from "../../../../support/contract-examples";
import {
  accepts,
  rejects,
  variant,
} from "../../../../support/schema-assertions";

describe("request rules of the contract", () => {
  const register = {
    kind: "register",
    app: "wiki",
    payload: {
      slug: "wiki",
      display_name: "Wiki",
      source_path: "/Workspace/Shared/wiki",
    },
    reason: "a small app",
  };

  it("requires app to equal payload.slug for a register request", () => {
    assert.ok(api.CreatePermissionRequest.safeParse(register).success);
    assert.equal(
      api.CreatePermissionRequest.safeParse({ ...register, app: "other" })
        .success,
      false,
    );
  });

  it("refuses a register request that names a memory budget: the platform sets one", () => {
    for (const memory_budget_mb of [128, 63, null]) {
      assert.equal(
        api.CreatePermissionRequest.safeParse({
          ...register,
          payload: { ...register.payload, memory_budget_mb },
        }).success,
        false,
        String(memory_budget_mb),
      );
    }
  });

  it("refuses a blank reason, a reason over 500 characters and an empty settings payload", () => {
    assert.equal(
      api.CreatePermissionRequest.safeParse({ ...register, reason: "  " })
        .success,
      false,
    );
    assert.equal(
      api.CreatePermissionRequest.safeParse({
        ...register,
        reason: "x".repeat(501),
      }).success,
      false,
    );
    const settings = {
      kind: "settings",
      app: "wiki",
      payload: {},
      reason: "why",
    };
    assert.equal(
      api.CreatePermissionRequest.safeParse(settings).success,
      false,
    );
    assert.ok(
      api.CreatePermissionRequest.safeParse({
        ...settings,
        payload: { slug: "wiki-2" },
      }).success,
    );
  });

  it("refuses a delegation request that asks for nothing or asks for false", () => {
    const delegation = {
      kind: "delegation",
      app: "wiki",
      payload: { obo: true },
      reason: "why",
    };
    assert.ok(api.CreatePermissionRequest.safeParse(delegation).success);
    assert.equal(
      api.CreatePermissionRequest.safeParse({ ...delegation, payload: {} })
        .success,
      false,
    );
    assert.equal(
      api.CreatePermissionRequest.safeParse({
        ...delegation,
        payload: { obo: false },
      }).success,
      false,
    );
  });

  it("refuses a slug, a memory budget and a source path outside the registration rules", () => {
    const base = {
      slug: "wiki",
      display_name: "Wiki",
      source_path: "/Workspace/x",
      memory_budget_mb: 256,
      owner: "a@example.com",
    };
    assert.ok(api.RegisterAppRequest.safeParse(base).success);
    // Left out, the app gets the platform's default budget.
    assert.ok(
      api.RegisterAppRequest.safeParse(without(base, "memory_budget_mb"))
        .success,
    );
    for (const bad of [
      { slug: "Wiki" },
      { slug: "-wiki" },
      { slug: "a".repeat(32) },
      { memory_budget_mb: 255 },
      { memory_budget_mb: 16385 },
      { source_path: "/tmp/x" },
      { owner: "Alice@example.com" },
    ]) {
      assert.equal(
        api.RegisterAppRequest.safeParse({ ...base, ...bad }).success,
        false,
        JSON.stringify(bad),
      );
    }
  });

  it("refuses an update with only a version, and admins with a non-user/group entry", () => {
    assert.equal(api.UpdateAppRequest.safeParse({ version: 1 }).success, false);
    assert.equal(
      api.PutAdminsRequest.safeParse({
        admins: [{ type: "everyone", name: "*" }],
        reason: "x",
      }).success,
      false,
    );
  });
});

describe("reasons", () => {
  const delegation = readExampleObject("DelegationReasonRequest");

  it("a delegation reason is not blank, at most 500 characters, and comes with a version", () => {
    accepts(api.DelegationReasonRequest, variant(delegation, { reason: "r" }));
    accepts(
      api.DelegationReasonRequest,
      variant(delegation, { reason: "r".repeat(500) }),
    );
    rejects(
      api.DelegationReasonRequest,
      variant(delegation, { reason: "r".repeat(501) }),
    );
    rejects(api.DelegationReasonRequest, variant(delegation, { reason: " " }));
    rejects(api.DelegationReasonRequest, { reason: "r" }, "no version");
  });

  it("a permission request reason is not blank and at most 500 characters", () => {
    const request = readExampleObject("CreatePermissionRequest");
    accepts(
      api.CreatePermissionRequest,
      variant(request, { reason: "r".repeat(500) }),
    );
    rejects(
      api.CreatePermissionRequest,
      variant(request, { reason: "r".repeat(501) }),
    );
    rejects(api.CreatePermissionRequest, variant(request, { reason: "" }));
  });

  it("the payload of a settings request follows the registration rules", () => {
    const settings = (payload: Record<string, unknown>) => ({
      kind: "settings",
      app: "wiki",
      payload,
      reason: "why",
    });
    accepts(api.CreatePermissionRequest, settings({ memory_budget_mb: 256 }));
    rejects(api.CreatePermissionRequest, settings({ memory_budget_mb: 255 }));
    rejects(api.CreatePermissionRequest, settings({ source_path: "/tmp" }));
    rejects(
      api.CreatePermissionRequest,
      settings({ slug: "wiki", is_admin: true }),
      "an unlisted payload field",
    );
  });

  it("the platform admins' reason is not blank", () => {
    const admins = readExampleObject("PutAdminsRequest");
    rejects(api.PutAdminsRequest, variant(admins, { reason: " " }));
  });
});
