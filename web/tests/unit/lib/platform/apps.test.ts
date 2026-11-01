import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { PlatformApp } from "@/lib/api/schemas";
import {
  EMPTY_REGISTER_FORM,
  appFieldsFormValues,
  appFieldsKey,
  appOwnerKey,
  filterApps,
  parseAppFieldsForm,
  parseOwnerForm,
  parseRegisterForm,
  renameOutcome,
} from "@/lib/platform/apps";

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

const APPS = [
  app(),
  app({
    slug: "reports",
    display_name: "Monthly Reports",
    owner: "bob@example.com",
  }),
  app({ slug: "hr-tools", display_name: "People", owner: "carol@example.com" }),
];

describe("filterApps", () => {
  it("returns every app for a blank query", () => {
    assert.equal(filterApps(APPS, "").length, 3);
    assert.equal(filterApps(APPS, "   ").length, 3);
  });

  it("matches slug, display name and owner, ignoring case", () => {
    assert.deepEqual(
      filterApps(APPS, "SALES").map((a) => a.slug),
      ["sales"],
    );
    assert.deepEqual(
      filterApps(APPS, "monthly").map((a) => a.slug),
      ["reports"],
    );
    assert.deepEqual(
      filterApps(APPS, "carol@").map((a) => a.slug),
      ["hr-tools"],
    );
  });

  it("keeps the order it was given", () => {
    assert.deepEqual(
      filterApps(APPS, "example.com").map((a) => a.slug),
      ["sales", "reports", "hr-tools"],
    );
  });

  it("returns nothing when nothing matches", () => {
    assert.deepEqual(filterApps(APPS, "zzz"), []);
  });
});

describe("parseRegisterForm", () => {
  const filled = {
    slug: "forecast",
    display_name: "Forecast",
    source_path: "/Workspace/Shared/apps/forecast",
    memory_budget_mb: "512",
    owner: "dave@example.com",
  };

  it("builds the body of POST /apps from the typed text", () => {
    assert.deepEqual(parseRegisterForm(filled, ["sales"]), {
      ok: true,
      value: {
        slug: "forecast",
        display_name: "Forecast",
        source_path: "/Workspace/Shared/apps/forecast",
        memory_budget_mb: 512,
        owner: "dave@example.com",
      },
    });
  });

  it("ignores spaces around the text", () => {
    const result = parseRegisterForm(
      { ...filled, slug: " forecast ", owner: " dave@example.com " },
      [],
    );
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.value.slug, "forecast");
      assert.equal(result.value.owner, "dave@example.com");
    }
  });

  it("names every field that is wrong at once", () => {
    const result = parseRegisterForm(EMPTY_REGISTER_FORM, []);
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.deepEqual(Object.keys(result.errors).toSorted(), [
        "display_name",
        "memory_budget_mb",
        "owner",
        "slug",
        "source_path",
      ]);
      assert.equal(result.errors.memory_budget_mb, "is required");
    }
  });

  it("keeps the contract's wording for the slug and the folder", () => {
    const result = parseRegisterForm(
      { ...filled, slug: "Bad Slug", source_path: "/tmp/x" },
      [],
    );
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(
        result.errors.slug ?? "",
        /lower-case letters, digits and -/,
      );
      assert.equal(result.errors.source_path, "must start with /Workspace/");
    }
  });

  it("refuses a budget that is not a whole number, and one out of range, with different words", () => {
    const typo = parseRegisterForm({ ...filled, memory_budget_mb: "5e2" }, []);
    const small = parseRegisterForm({ ...filled, memory_budget_mb: "10" }, []);
    assert.equal(typo.ok, false);
    assert.equal(small.ok, false);
    if (!typo.ok && !small.ok) {
      assert.equal(
        typo.errors.memory_budget_mb,
        "must be a whole number of MB",
      );
      assert.equal(small.errors.memory_budget_mb, "must be at least 64");
    }
  });

  it("refuses an upper-case owner instead of quietly changing it", () => {
    const result = parseRegisterForm(
      { ...filled, owner: "Dave@Example.com" },
      [],
    );
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.errors.owner, "must be lower case");
  });

  it("says a slug that an app already has is taken", () => {
    const result = parseRegisterForm({ ...filled, slug: "sales" }, ["sales"]);
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.deepEqual(result.errors, {
        slug: "slug 'sales' is already taken",
      });
    }
  });
});

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

describe("renameOutcome", () => {
  it("says the row holds the new slug and that the swap decides when the old URL stops", () => {
    const outcome = renameOutcome("sales", "revenue");
    assert.equal(outcome.tone, "success");
    assert.equal(outcome.title, "Renamed sales to revenue");
    assert.match(outcome.detail ?? "", /\/revenue\//);
    // The swap is health-gated (design 5.5): the answer of the PATCH is not the moment the route changes.
    assert.match(outcome.detail ?? "", /passes its health check/);
    assert.doesNotMatch(outcome.detail ?? "", /is now/);
  });
});
