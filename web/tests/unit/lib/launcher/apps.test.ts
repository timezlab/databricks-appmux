import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { AppList } from "@/lib/api";
import type { AppSummary } from "@/lib/api";
import { readAppParam } from "@/lib/app/params";
import {
  APP_FILTER_MIN_COUNT,
  filterAppRows,
  launcherAppRows,
  shouldExplainOpenModes,
  shouldOfferAppFilter,
  visibleAppRows,
} from "@/lib/launcher/apps";
import { readExample } from "../../../support/contract-examples";

function summary(overrides: Partial<AppSummary> = {}): AppSummary {
  return {
    slug: "sales",
    display_name: "Sales dashboard",
    state: "running",
    permission: "CAN_USE",
    open_modes: [],
    ...overrides,
  };
}

function onlyRow(overrides: Partial<AppSummary> = {}) {
  const [row] = launcherAppRows([summary(overrides)]);
  assert.ok(row);
  return row;
}

describe("the Open action (design 9.1: one Open, or two modes for a platform admin)", () => {
  // The link itself is drawn by AppLink / openAppWithMode from the slug, after
  // slug-link.ts has checked it; the row only says which control to draw.
  it("a running app with no modes has one Open link", () => {
    assert.deepEqual(onlyRow().open, { kind: "open" });
  });

  it("an OBO app a platform admin has no grant on offers both modes, without token first", () => {
    const row = onlyRow({ open_modes: ["as_user", "without_token"] });
    assert.deepEqual(row.open, {
      kind: "choose-mode",
      modes: ["without_token", "as_user"],
    });
  });

  it("follows the modes the server sent: a single mode is offered alone", () => {
    const row = onlyRow({ open_modes: ["without_token"] });
    assert.deepEqual(row.open, {
      kind: "choose-mode",
      modes: ["without_token"],
    });
  });

  it("never offers a mode twice", () => {
    const row = onlyRow({
      open_modes: ["as_user", "as_user", "without_token"],
    });
    assert.deepEqual(row.open, {
      kind: "choose-mode",
      modes: ["without_token", "as_user"],
    });
  });

  it("offers no Open while the route is closed, and says why", () => {
    for (const state of ["stopped", "starting", "crashed"] as const) {
      const { open } = onlyRow({
        state,
        open_modes: ["without_token", "as_user"],
      });
      assert.equal(open.kind, "unavailable", state);
      if (open.kind === "unavailable") {
        assert.match(open.reason, /\S/);
      }
    }
  });

  it("the three reasons differ, so a person can tell the states apart", () => {
    const reasons = (["stopped", "starting", "crashed"] as const).map(
      (state) => {
        const { open } = onlyRow({ state });
        return open.kind === "unavailable" ? open.reason : "";
      },
    );
    assert.equal(new Set(reasons).size, 3);
  });

  it("tells a person who cannot manage a stopped or crashed app to ask its admin", () => {
    for (const state of ["stopped", "crashed"] as const) {
      const { open } = onlyRow({ state, permission: "CAN_USE" });
      assert.equal(open.kind, "unavailable", state);
      if (open.kind === "unavailable") {
        assert.match(open.reason, /Ask the app admin/, state);
      }
    }
  });

  it("points a person who manages a stopped or crashed app to Manage, where it is started", () => {
    for (const state of ["stopped", "crashed"] as const) {
      const { open } = onlyRow({ state, permission: "CAN_MANAGE" });
      assert.equal(open.kind, "unavailable", state);
      if (open.kind === "unavailable") {
        assert.match(open.reason, /from Manage/, state);
        assert.doesNotMatch(open.reason, /Ask the app admin/, state);
      }
    }
  });

  it("an app whose slug fails the contract pattern gets no link at all", () => {
    for (const slug of [
      "../admin",
      "../x",
      "_appmux",
      "Sales",
      "a/b",
      "a b",
      "x&y=1",
      "//evil.example",
      "",
    ]) {
      const row = onlyRow({
        slug,
        permission: "CAN_MANAGE",
        open_modes: ["without_token", "as_user"],
      });
      assert.equal(row.open.kind, "unavailable", JSON.stringify(slug));
      assert.equal(row.manageHref, null, JSON.stringify(slug));
    }
  });
});

describe("the Manage link", () => {
  it("is offered to CAN_MANAGE only", () => {
    assert.equal(onlyRow({ permission: "CAN_USE" }).manageHref, null);
    assert.equal(
      onlyRow({ permission: "CAN_MANAGE" }).manageHref,
      "/app/?slug=sales",
    );
  });

  it("is offered for an app that is not running, because that is when an admin needs it", () => {
    const row = onlyRow({ permission: "CAN_MANAGE", state: "crashed" });
    assert.equal(row.manageHref, "/app/?slug=sales");
  });

  it("is built from a valid slug, with nothing else in the query", () => {
    assert.equal(
      onlyRow({ slug: "my-app", permission: "CAN_MANAGE" }).manageHref,
      "/app/?slug=my-app",
    );
  });

  it("is what the app page reads: its parameter gives back the slug", () => {
    const { manageHref } = onlyRow({
      slug: "my-app",
      permission: "CAN_MANAGE",
    });
    assert.ok(manageHref);
    const { searchParams } = new URL(manageHref, "https://example.test");
    assert.deepEqual(readAppParam(searchParams), {
      status: "valid",
      slug: "my-app",
    });
  });
});

describe("the explanation of the two open modes (it depends on the whole list)", () => {
  const plain = launcherAppRows([summary()]);
  const choosing = launcherAppRows([
    summary({ slug: "docs" }),
    summary({ open_modes: ["without_token", "as_user"] }),
  ]);

  it("is offered only when some app offers both modes", () => {
    assert.equal(shouldExplainOpenModes(plain), false);
    assert.equal(shouldExplainOpenModes([]), false);
    assert.equal(shouldExplainOpenModes(choosing), true);
  });

  it("is not offered for an app that offers one mode only: there is nothing to tell apart", () => {
    const single = launcherAppRows([
      summary({ open_modes: ["without_token"] }),
    ]);
    assert.equal(shouldExplainOpenModes(single), false);
  });

  it("does not explain modes for an app whose choice is closed (not running)", () => {
    const stopped = launcherAppRows([
      summary({ state: "stopped", open_modes: ["without_token", "as_user"] }),
    ]);
    assert.equal(shouldExplainOpenModes(stopped), false);
  });
});

describe("the rows", () => {
  const slugsOf = (rows: readonly { slug: string }[]) =>
    rows.map((row) => row.slug);

  it("are sorted by display name, whatever order the server sent", () => {
    const rows = launcherAppRows([
      summary({ slug: "b", display_name: "Beta" }),
      summary({ slug: "a", display_name: "Gamma" }),
      summary({ slug: "c", display_name: "Alpha" }),
    ]);
    assert.deepEqual(slugsOf(rows), ["c", "b", "a"]);
  });

  it("sort names without regard to case, so 'alpha' does not follow every capital letter", () => {
    const rows = launcherAppRows([
      summary({ slug: "z", display_name: "Zulu" }),
      summary({ slug: "a", display_name: "alpha" }),
      summary({ slug: "m", display_name: "Mike" }),
    ]);
    assert.deepEqual(slugsOf(rows), ["a", "m", "z"]);
  });

  it("sort the numbers in names as numbers: App 2 comes before App 10", () => {
    const rows = launcherAppRows([
      summary({ slug: "app-10", display_name: "App 10" }),
      summary({ slug: "app-2", display_name: "App 2" }),
      summary({ slug: "app-1", display_name: "App 1" }),
    ]);
    assert.deepEqual(slugsOf(rows), ["app-1", "app-2", "app-10"]);
  });

  it("put the same display name in slug order", () => {
    const rows = launcherAppRows([
      summary({ slug: "b", display_name: "Wiki" }),
      summary({ slug: "a", display_name: "Wiki" }),
    ]);
    assert.deepEqual(slugsOf(rows), ["a", "b"]);
  });

  it("put what needs attention first: crashed, starting, stopped, then running, each by name", () => {
    const rows = launcherAppRows([
      summary({ slug: "a-run", display_name: "Alpha", state: "running" }),
      summary({ slug: "z-stop", display_name: "Zulu", state: "stopped" }),
      summary({ slug: "b-stop", display_name: "Bravo", state: "stopped" }),
      summary({ slug: "m-start", display_name: "Mike", state: "starting" }),
      summary({ slug: "y-crash", display_name: "Yankee", state: "crashed" }),
    ]);
    assert.deepEqual(slugsOf(rows), [
      "y-crash",
      "m-start",
      "b-stop",
      "z-stop",
      "a-run",
    ]);
  });

  it("carry the name and the state as sent; the name stays text", () => {
    const row = onlyRow({ display_name: "<img src=x onerror=alert(1)>" });
    assert.equal(row.displayName, "<img src=x onerror=alert(1)>");
    assert.equal(row.state, "running");
  });

  it("say the person's level on each app in words", () => {
    assert.equal(onlyRow({ permission: "CAN_USE" }).levelLabel, "Can use");
    assert.equal(
      onlyRow({ permission: "CAN_MANAGE" }).levelLabel,
      "Can manage",
    );
  });

  it("read the contract's AppList example", () => {
    const rows = launcherAppRows(AppList.parse(readExample("AppList")).items);
    assert.deepEqual(
      rows.map((row) => [row.slug, row.open.kind, row.manageHref]),
      [
        // Stopped, so it comes first.
        ["docs", "unavailable", "/app/?slug=docs"],
        ["sales", "choose-mode", "/app/?slug=sales"],
        ["reports", "open", "/app/?slug=reports"],
      ],
    );
  });
});

describe("the filter box", () => {
  const rows = launcherAppRows([
    summary({ slug: "sales", display_name: "Sales dashboard" }),
    summary({ slug: "docs", display_name: "Team docs" }),
    summary({ slug: "wiki-2", display_name: "Wiki" }),
  ]);
  const slugs = (found: readonly { slug: string }[]) =>
    found.map((row) => row.slug);

  it("is offered only when the list is long enough to need it", () => {
    assert.equal(shouldOfferAppFilter(APP_FILTER_MIN_COUNT - 1), false);
    assert.equal(shouldOfferAppFilter(APP_FILTER_MIN_COUNT), true);
    assert.equal(shouldOfferAppFilter(100), true);
  });

  it("an empty or blank query keeps every row", () => {
    assert.deepEqual(slugs(filterAppRows(rows, "")), [
      "sales",
      "docs",
      "wiki-2",
    ]);
    assert.deepEqual(slugs(filterAppRows(rows, "   ")), [
      "sales",
      "docs",
      "wiki-2",
    ]);
  });

  it("matches the name or the slug, ignoring case and surrounding spaces", () => {
    assert.deepEqual(slugs(filterAppRows(rows, " TEAM ")), ["docs"]);
    assert.deepEqual(slugs(filterAppRows(rows, "wiki-2")), ["wiki-2"]);
    assert.deepEqual(slugs(filterAppRows(rows, "s")), ["sales", "docs"]);
  });

  it("a query that matches nothing gives an empty list", () => {
    assert.deepEqual(filterAppRows(rows, "zzz"), []);
  });
});

describe("the rows shown (the filter applies only while its box is on screen)", () => {
  const names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta"];
  function manyRows(count: number) {
    return launcherAppRows(
      Array.from({ length: count }, (_, index) =>
        summary({
          slug: `app-${index}`,
          display_name: names[index % names.length] ?? "App",
        }),
      ),
    );
  }

  it("filters when the box is offered", () => {
    const rows = manyRows(APP_FILTER_MIN_COUNT);
    const shown = visibleAppRows(rows, "alpha");
    assert.ok(shown.length > 0 && shown.length < rows.length);
    assert.ok(shown.every((row) => row.displayName === "Alpha"));
  });

  it("shows every app once the list is too short for the box, whatever query was typed", () => {
    // A poll can shrink the list under the threshold while a query is typed;
    // the box is gone then, so the query must not keep hiding apps.
    const rows = manyRows(APP_FILTER_MIN_COUNT - 1);
    assert.equal(visibleAppRows(rows, "alpha").length, rows.length);
    assert.equal(visibleAppRows(rows, "no app has this").length, rows.length);
  });

  it("an empty query shows every app", () => {
    const rows = manyRows(APP_FILTER_MIN_COUNT);
    assert.equal(visibleAppRows(rows, "").length, rows.length);
  });
});
