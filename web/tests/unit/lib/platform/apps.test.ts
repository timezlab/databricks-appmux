import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { PlatformApp } from "@/lib/api/schemas";
import {
  EMPTY_REGISTER_FORM,
  filterApps,
  parseRegisterForm,
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
