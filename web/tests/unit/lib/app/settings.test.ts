import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { PlatformApp } from "@/lib/api/schemas";
import type { Actor } from "@/lib/app/actions";
import { REASON_NOT_MANAGER, REASON_UNKNOWN_ACTOR } from "@/lib/app/actions";
import {
  NODE_MEMORY_MIN_MB,
  appFieldsFormValues,
  appFieldsKey,
  appOwnerKey,
  isDeleteConfirmed,
  nodeMemoryHint,
  parseAppFieldsForm,
  parseOwnerForm,
  renameMessage,
  renameWarning,
  settingsAccess,
} from "@/lib/app/settings";

// The forms take what an AppDetail and a PlatformApp share, so either fits.
function app(overrides: Partial<PlatformApp> = {}): PlatformApp {
  return {
    slug: "sales",
    display_name: "Sales dashboard",
    source_path: "/Workspace/Shared/apps/sales",
    memory_budget_mb: 512,
    owner: "alice@example.com",
    obo_delegated: false,
    shared_sp_delegated: false,
    egress: [],
    version: 7,
    ...overrides,
  };
}

function actor(overrides: Partial<Actor> = {}): Actor {
  return {
    email: "bob@example.com",
    isPlatformAdmin: false,
    permission: "CAN_MANAGE",
    canManage: true,
    ...overrides,
  };
}

describe("parseAppFieldsForm", () => {
  const current = app();

  it("starts from the app's current values", () => {
    assert.deepEqual(appFieldsFormValues(current), {
      slug: "sales",
      display_name: "Sales dashboard",
      source_path: "/Workspace/Shared/apps/sales",
      memory_budget_mb: "512",
    });
  });

  it("sends only what changed, with the version the page holds", () => {
    const values = {
      ...appFieldsFormValues(current),
      memory_budget_mb: "1024",
    };
    assert.deepEqual(parseAppFieldsForm(current, values, ["reports"]), {
      ok: true,
      value: { version: 7, memory_budget_mb: 1024 },
    });
  });

  it("can change several fields at once", () => {
    const values = {
      ...appFieldsFormValues(current),
      display_name: " Sales ",
      source_path: "/Workspace/Shared/apps/sales-v2",
    };
    assert.deepEqual(parseAppFieldsForm(current, values, []), {
      ok: true,
      value: {
        version: 7,
        display_name: "Sales",
        source_path: "/Workspace/Shared/apps/sales-v2",
      },
    });
  });

  it("refuses a form where nothing changed, since the server refuses a body with no field", () => {
    assert.deepEqual(
      parseAppFieldsForm(current, appFieldsFormValues(current), []),
      { ok: false, errors: { _form: "Change at least one field" } },
    );
  });

  it("does not hold an unchanged field to the rules, only the changed ones", () => {
    const odd = app({ source_path: "/legacy/path" });
    const values = { ...appFieldsFormValues(odd), display_name: "New name" };
    assert.deepEqual(parseAppFieldsForm(odd, values, []), {
      ok: true,
      value: { version: 7, display_name: "New name" },
    });
  });

  it("checks a changed slug against the other apps, not against itself", () => {
    const rename = { ...appFieldsFormValues(current), slug: "reports" };
    assert.deepEqual(parseAppFieldsForm(current, rename, ["reports"]), {
      ok: false,
      errors: { slug: "slug 'reports' is already taken" },
    });
    assert.equal(parseAppFieldsForm(current, rename, []).ok, true);
  });

  it("refuses a blanked name and a budget that is not a number", () => {
    const values = {
      ...appFieldsFormValues(current),
      display_name: "  ",
      memory_budget_mb: "lots",
    };
    const result = parseAppFieldsForm(current, values, []);
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.errors.display_name, "must not be blank");
      assert.equal(
        result.errors.memory_budget_mb,
        "must be a whole number of MB",
      );
    }
  });
});

describe("parseOwnerForm", () => {
  const current = app();

  it("builds the body of PUT /apps/{app}/owner", () => {
    assert.deepEqual(parseOwnerForm(current, " bob@example.com "), {
      ok: true,
      value: { version: 7, owner: "bob@example.com" },
    });
  });

  it("refuses the owner it already has, an address that is not one, and a blank", () => {
    assert.deepEqual(parseOwnerForm(current, "alice@example.com"), {
      ok: false,
      errors: { owner: "is already the owner" },
    });
    const bad = parseOwnerForm(current, "bob");
    assert.equal(bad.ok, false);
    const blank = parseOwnerForm(current, " ");
    assert.equal(blank.ok, false);
  });
});

describe("form keys", () => {
  it("change when a field the form shows changes, and not when only the version moves", () => {
    const before = app();
    assert.equal(appFieldsKey(before), appFieldsKey(app({ version: 9 })));
    assert.notEqual(
      appFieldsKey(before),
      appFieldsKey(app({ display_name: "x" })),
    );
    assert.notEqual(
      appFieldsKey(before),
      appFieldsKey(app({ memory_budget_mb: 256 })),
    );
    assert.equal(appOwnerKey(before), appOwnerKey(app({ version: 9 })));
    assert.notEqual(
      appOwnerKey(before),
      appOwnerKey(app({ owner: "bob@example.com" })),
    );
  });
});

describe("renaming", () => {
  it("warns before the save that the address moves, and when the old one stops", () => {
    const warning = renameWarning("sales", "revenue");
    assert.match(warning, /\/sales\//);
    assert.match(warning, /\/revenue\//);
    assert.match(warning, /passes its health check/);
  });

  it("says the row holds the new slug and that the swap decides when the old URL stops", () => {
    const message = renameMessage("sales", "revenue");
    assert.match(message, /^Renamed sales to revenue\./);
    assert.match(message, /\/revenue\//);
    // The swap is health-gated (design 5.5): the answer of the PATCH is not the moment the route changes.
    assert.match(message, /passes its health check/);
    assert.doesNotMatch(message, /is now/);
  });
});

describe("settingsAccess", () => {
  it("lets a platform admin change every field, the owner, and delete the app", () => {
    const access = settingsAccess(actor({ isPlatformAdmin: true }));
    assert.deepEqual(access, {
      fields: { kind: "edit" },
      owner: { kind: "edit" },
      delete: { kind: "edit" },
    });
  });

  it("shows an app admin the fields read-only with a request, and the owner and delete as a platform admin's", () => {
    const access = settingsAccess(actor());
    assert.equal(access.fields.kind, "request");
    assert.equal(access.owner.kind, "read-only");
    assert.equal(access.delete.kind, "read-only");
    if (
      access.owner.kind === "read-only" &&
      access.delete.kind === "read-only"
    ) {
      assert.match(access.owner.reason, /platform admin/);
      assert.match(access.delete.reason, /platform admin/);
    }
  });

  it("keeps everything read-only while the actor is unknown, and for a person who does not manage the app", () => {
    const unknown = settingsAccess(null);
    const visitor = settingsAccess(
      actor({ permission: "CAN_USE", canManage: false }),
    );
    for (const part of ["fields", "owner", "delete"] as const) {
      assert.deepEqual(unknown[part], {
        kind: "read-only",
        reason: REASON_UNKNOWN_ACTOR,
      });
      assert.deepEqual(visitor[part], {
        kind: "read-only",
        reason: REASON_NOT_MANAGER,
      });
    }
  });
});

describe("nodeMemoryHint", () => {
  it("warns about a Node.js app with a budget under the suggested memory", () => {
    const hint = nodeMemoryHint("node", "512");
    assert.match(hint ?? "", /Node\.js/);
    assert.match(hint ?? "", new RegExp(`${NODE_MEMORY_MIN_MB} MB`));
  });

  it("says nothing at or above it, for Python, an unknown runtime, or a budget that is not a number", () => {
    assert.equal(nodeMemoryHint("node", String(NODE_MEMORY_MIN_MB)), null);
    assert.equal(nodeMemoryHint("node", "1024"), null);
    assert.equal(nodeMemoryHint("python", "256"), null);
    assert.equal(nodeMemoryHint(null, "256"), null);
    assert.equal(nodeMemoryHint("node", "lots"), null);
  });
});

describe("isDeleteConfirmed", () => {
  it("needs the slug exactly, spaces around it aside", () => {
    assert.equal(isDeleteConfirmed(" sales ", "sales"), true);
    assert.equal(isDeleteConfirmed("Sales", "sales"), false);
    assert.equal(isDeleteConfirmed("sale", "sales"), false);
    assert.equal(isDeleteConfirmed("", "sales"), false);
  });
});
