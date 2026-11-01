import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { PlatformApp } from "@/lib/api/schemas";
import {
  delegationActionProblem,
  delegationCell,
  parseDelegationReason,
} from "@/lib/platform/delegations";

function app(overrides: Partial<PlatformApp> = {}): PlatformApp {
  return {
    slug: "sales",
    display_name: "Sales",
    source_path: "/Workspace/Shared/apps/sales",
    memory_budget_mb: 512,
    owner: "alice@example.com",
    obo_delegated: false,
    shared_sp_delegated: false,
    egress: [],
    version: 3,
    ...overrides,
  };
}

describe("delegationCell", () => {
  it("offers Grant for a credential the app does not have", () => {
    assert.deepEqual(delegationCell(app(), "obo", false), {
      kind: "obo",
      isGranted: false,
      stateText: "Not allowed",
      action: "grant",
      blockedReason: null,
    });
  });

  it("offers Revoke for a credential the app has", () => {
    const cell = delegationCell(app({ obo_delegated: true }), "obo", false);
    assert.equal(cell.isGranted, true);
    assert.equal(cell.stateText, "Allowed");
    assert.equal(cell.action, "revoke");
  });

  it("blocks a shared-SP grant while the master switch is off, and says which setting", () => {
    const cell = delegationCell(app(), "shared_sp", false);
    assert.equal(cell.action, null);
    assert.match(cell.blockedReason ?? "", /shared_sp_delegation_allowed/);
  });

  it("allows a shared-SP grant while the switch is on", () => {
    assert.equal(delegationCell(app(), "shared_sp", true).action, "grant");
  });

  it("always allows a revoke, switch or no switch", () => {
    const cell = delegationCell(
      app({ shared_sp_delegated: true }),
      "shared_sp",
      false,
    );
    assert.equal(cell.action, "revoke");
    assert.equal(cell.blockedReason, null);
  });

  it("reads each kind from its own flag", () => {
    const both = app({ obo_delegated: true });
    assert.equal(delegationCell(both, "obo", true).isGranted, true);
    assert.equal(delegationCell(both, "shared_sp", true).isGranted, false);
  });
});

describe("delegationActionProblem", () => {
  it("is silent while the action still applies", () => {
    assert.equal(delegationActionProblem(app(), "obo", "grant", false), null);
    assert.equal(
      delegationActionProblem(
        app({ obo_delegated: true }),
        "obo",
        "revoke",
        false,
      ),
      null,
    );
  });

  it("says so when another admin already did it", () => {
    assert.match(
      delegationActionProblem(
        app({ obo_delegated: true }),
        "obo",
        "grant",
        false,
      ) ?? "",
      /already allowed/i,
    );
    assert.match(
      delegationActionProblem(app(), "obo", "revoke", false) ?? "",
      /not allowed/i,
    );
  });

  it("says so when the master switch was turned off after the panel opened", () => {
    assert.match(
      delegationActionProblem(app(), "shared_sp", "grant", false) ?? "",
      /shared_sp_delegation_allowed/,
    );
  });
});

describe("parseDelegationReason", () => {
  it("builds the body of the PUT, or the query of the DELETE, with the latest version", () => {
    assert.deepEqual(
      parseDelegationReason(
        app({ version: 9 }),
        "  needs the viewer's rights  ",
      ),
      {
        ok: true,
        value: { version: 9, reason: "needs the viewer's rights" },
      },
    );
  });

  it("requires a reason of at most 500 characters", () => {
    assert.deepEqual(parseDelegationReason(app(), "   "), {
      ok: false,
      errors: { reason: "must not be blank" },
    });
    assert.deepEqual(parseDelegationReason(app(), "x".repeat(501)), {
      ok: false,
      errors: { reason: "must be at most 500 characters" },
    });
    assert.equal(parseDelegationReason(app(), "x".repeat(500)).ok, true);
  });
});
