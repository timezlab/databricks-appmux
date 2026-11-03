/**
 * The query strings of the read routes added for the admin UI (admin-api.md,
 * "Addendum: routes and fields for the admin UI overhaul", Routes table). Every
 * limit is a literal from that table on purpose: a test that read the schema's
 * own constant would follow a mutation of it.
 */
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { accepts, rejects } from "../../../../support/schema-assertions";

describe("PrincipalQuery (GET /principals?q=&types=&limit=)", () => {
  it("takes every parameter as optional", () => {
    accepts(api.PrincipalQuery, {});
    accepts(api.PrincipalQuery, {
      q: "ali",
      types: ["user", "group"],
      limit: 10,
    });
  });

  it("takes q of at most 64 characters, of A-Z a-z 0-9 space . _ @ + -", () => {
    accepts(api.PrincipalQuery, { q: "" }, "an empty q reads no directory");
    accepts(api.PrincipalQuery, { q: "a".repeat(64) });
    rejects(api.PrincipalQuery, { q: "a".repeat(65) });
    accepts(api.PrincipalQuery, { q: "Alice B. c_d@example.com+x-y" });
    // The text goes into a SCIM filter on the server: these are what it refuses.
    for (const bad of [
      'a"b',
      "a'b",
      "a\\b",
      "a*b",
      "a,b",
      "a%b",
      "a\nb",
      "é",
    ]) {
      rejects(api.PrincipalQuery, { q: bad }, JSON.stringify(bad));
    }
    rejects(api.PrincipalQuery, { q: "ok\n" }, "a trailing newline");
  });

  it("takes types as a non-empty list of the four grant types", () => {
    for (const type of ["user", "group", "service_principal", "everyone"]) {
      accepts(api.PrincipalQuery, { types: [type] }, type);
    }
    accepts(api.PrincipalQuery, {
      types: ["user", "group", "service_principal", "everyone"],
    });
    rejects(api.PrincipalQuery, { types: [] }, "an empty list");
    rejects(api.PrincipalQuery, { types: ["admin"] });
    rejects(api.PrincipalQuery, { types: "user,group" }, "a comma string");
    rejects(api.PrincipalQuery, { types: ["user,group"] });
  });

  it("takes limit as a whole number from 1 to 50", () => {
    accepts(api.PrincipalQuery, { limit: 1 });
    accepts(api.PrincipalQuery, { limit: 50 });
    for (const bad of [0, -1, 51, 1.5, Number.NaN, "10"]) {
      rejects(api.PrincipalQuery, { limit: bad }, String(bad));
    }
  });

  it("refuses a parameter the route does not have", () => {
    rejects(api.PrincipalQuery, { app: "sales" });
  });
});

describe("WorkspaceBrowseQuery (GET /workspace/browse?path=)", () => {
  it("takes no path, which lists the roots", () => {
    accepts(api.WorkspaceBrowseQuery, {});
    accepts(api.WorkspaceBrowseQuery, { path: "/Workspace/Shared/sales" });
  });

  it("leaves the /Workspace/ rule to the server", () => {
    accepts(api.WorkspaceBrowseQuery, { path: "/tmp" });
    accepts(api.WorkspaceBrowseQuery, { path: "/Workspace/a/../b" });
    accepts(api.WorkspaceBrowseQuery, { path: "" });
  });

  it("takes at most 512 characters, counted as the server counts them", () => {
    accepts(api.WorkspaceBrowseQuery, { path: "a".repeat(512) });
    rejects(api.WorkspaceBrowseQuery, { path: "a".repeat(513) });
    // 512 characters of two UTF-16 units each: the server accepts them.
    accepts(api.WorkspaceBrowseQuery, { path: "\u{1F4C1}".repeat(512) });
    rejects(api.WorkspaceBrowseQuery, { path: "\u{1F4C1}".repeat(513) });
  });

  it("refuses a control character", () => {
    for (const bad of ["a\nb", "a\u0000b", "a\tb", "a\u007fb", "a\u0085b"]) {
      rejects(api.WorkspaceBrowseQuery, { path: bad }, JSON.stringify(bad));
    }
    accepts(api.WorkspaceBrowseQuery, { path: "/Workspace/my folder/é" });
  });

  it("refuses what is not text, and a parameter the route does not have", () => {
    rejects(api.WorkspaceBrowseQuery, { path: 5 });
    rejects(api.WorkspaceBrowseQuery, { path: null });
    rejects(api.WorkspaceBrowseQuery, { folder: "/Workspace" });
  });
});

describe("AppLogsQuery (GET /apps/{app}/logs?after=&limit=)", () => {
  it("takes every parameter as optional", () => {
    accepts(api.AppLogsQuery, {});
    accepts(api.AppLogsQuery, { after: 12, limit: 500 });
  });

  it("takes after as a whole number from 0, a seq the client already has", () => {
    accepts(api.AppLogsQuery, { after: 0 });
    accepts(api.AppLogsQuery, { after: 1_000_000 });
    for (const bad of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, "3"]) {
      rejects(api.AppLogsQuery, { after: bad }, String(bad));
    }
  });

  it("takes limit as a whole number from 1 to 1000", () => {
    accepts(api.AppLogsQuery, { limit: 1 });
    accepts(api.AppLogsQuery, { limit: 1000 });
    for (const bad of [0, -1, 1001, 1.5, Number.NaN, "10"]) {
      rejects(api.AppLogsQuery, { limit: bad }, String(bad));
    }
  });

  it("refuses a parameter the route does not have", () => {
    rejects(api.AppLogsQuery, { since: 3 });
  });
});

describe("ResourceSuggestionQuery (GET /resources/suggestions?type=&q=&limit=)", () => {
  it("needs a type, which the server checks against the catalogue", () => {
    accepts(api.ResourceSuggestionQuery, { type: "sql_warehouse" });
    // The catalogue is data (GET /resources/types), so the client holds no copy of its names.
    accepts(api.ResourceSuggestionQuery, { type: "a_type_added_later" });
    rejects(api.ResourceSuggestionQuery, {}, "type is missing");
    rejects(api.ResourceSuggestionQuery, { type: "" });
    rejects(api.ResourceSuggestionQuery, { type: 3 });
  });

  it("takes q of at most 256 characters with no control character", () => {
    const type = "secret";
    accepts(api.ResourceSuggestionQuery, { type, q: "" });
    accepts(api.ResourceSuggestionQuery, { type, q: "my-scope/prefix" });
    accepts(api.ResourceSuggestionQuery, { type, q: "a".repeat(256) });
    rejects(api.ResourceSuggestionQuery, { type, q: "a".repeat(257) });
    accepts(api.ResourceSuggestionQuery, {
      type,
      q: "\u{1F511}".repeat(256),
    });
    for (const bad of ["a\nb", "a\u0000b", "a\tb", "a\u007fb"]) {
      rejects(
        api.ResourceSuggestionQuery,
        { type, q: bad },
        JSON.stringify(bad),
      );
    }
  });

  it("takes limit as a whole number from 1 to 50", () => {
    const type = "job";
    accepts(api.ResourceSuggestionQuery, { type, limit: 1 });
    accepts(api.ResourceSuggestionQuery, { type, limit: 50 });
    for (const bad of [0, -1, 51, 1.5, Number.NaN, "20"]) {
      rejects(api.ResourceSuggestionQuery, { type, limit: bad }, String(bad));
    }
  });

  it("refuses a parameter the route does not have", () => {
    rejects(api.ResourceSuggestionQuery, { type: "job", app: "sales" });
  });
});

describe("MetricsBreakdownQuery (GET /metrics/breakdown?from=&to=&step=&limit=)", () => {
  it("takes every parameter as optional", () => {
    accepts(api.MetricsBreakdownQuery, {});
    accepts(api.MetricsBreakdownQuery, {
      from: "2026-11-03T08:00:00Z",
      to: "2026-11-03T09:00:00.000Z",
      step: "5m",
      limit: 8,
    });
  });

  it("takes step from the four the contract lists", () => {
    for (const step of ["1m", "5m", "15m", "1h"]) {
      accepts(api.MetricsBreakdownQuery, { step }, step);
    }
    for (const bad of ["10m", "1d", "", "1M"]) {
      rejects(api.MetricsBreakdownQuery, { step: bad }, bad);
    }
  });

  it("takes limit as a whole number from 1 to 20", () => {
    accepts(api.MetricsBreakdownQuery, { limit: 1 });
    accepts(api.MetricsBreakdownQuery, { limit: 20 });
    for (const bad of [0, -1, 21, 1.5, Number.NaN, "8"]) {
      rejects(api.MetricsBreakdownQuery, { limit: bad }, String(bad));
    }
  });

  it("wants ISO 8601 UTC times, and refuses an app filter, which this route does not have", () => {
    rejects(api.MetricsBreakdownQuery, { from: "yesterday" });
    rejects(api.MetricsBreakdownQuery, { to: "2026-11-03T08:00:00+02:00" });
    rejects(api.MetricsBreakdownQuery, { app: "sales" });
  });
});
