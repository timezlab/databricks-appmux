/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
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

describe("registration fields", () => {
  const register = readExampleObject("RegisterAppRequest");

  it("slug: letters, digits and -, starting with a letter or digit, at most 31", () => {
    for (const good of ["a", "0", "a-b", "a".repeat(31)]) {
      accepts(api.RegisterAppRequest, variant(register, { slug: good }), good);
    }
    for (const bad of ["", "-a", "A", "a_b", "a b", "a".repeat(32)]) {
      rejects(api.RegisterAppRequest, variant(register, { slug: bad }), bad);
    }
  });

  it("memory budget: 64 to 16384, an integer, and optional (the platform default)", () => {
    for (const good of [64, 16384, null]) {
      accepts(
        api.RegisterAppRequest,
        variant(register, { memory_budget_mb: good }),
        String(good),
      );
    }
    accepts(api.RegisterAppRequest, without(register, "memory_budget_mb"));
    for (const bad of [63, 16385, 64.5, "64"]) {
      rejects(
        api.RegisterAppRequest,
        variant(register, { memory_budget_mb: bad }),
        String(bad),
      );
    }
  });

  it("source path: starts with /Workspace/", () => {
    accepts(
      api.RegisterAppRequest,
      variant(register, { source_path: "/Workspace/a" }),
    );
    for (const bad of [
      "/Workspace",
      "Workspace/a",
      "/workspace/a",
      "/tmp/a",
      "",
    ]) {
      rejects(
        api.RegisterAppRequest,
        variant(register, { source_path: bad }),
        bad,
      );
    }
  });

  it("display name: not blank; owner: a lower-case email", () => {
    accepts(api.RegisterAppRequest, variant(register, { display_name: "x" }));
    for (const bad of ["", " ", "\t\n"]) {
      rejects(api.RegisterAppRequest, variant(register, { display_name: bad }));
    }
    accepts(
      api.RegisterAppRequest,
      variant(register, { owner: "a.b@example.com" }),
    );
    for (const bad of ["Alice@example.com", "alice", "alice@", ""]) {
      rejects(api.RegisterAppRequest, variant(register, { owner: bad }), bad);
    }
  });

  it("an app update needs a field besides version, and its fields follow the same rules", () => {
    rejects(api.UpdateAppRequest, { version: 1 });
    rejects(api.UpdateAppRequest, { version: 1, slug: null });
    rejects(api.UpdateAppRequest, { slug: "a" }, "no version");
    accepts(api.UpdateAppRequest, { version: 1, slug: "a".repeat(31) });
    rejects(api.UpdateAppRequest, { version: 1, slug: "a".repeat(32) });
    accepts(api.UpdateAppRequest, { version: 1, memory_budget_mb: 64 });
    rejects(api.UpdateAppRequest, { version: 1, memory_budget_mb: 63 });
    accepts(api.UpdateAppRequest, { version: 1, memory_budget_mb: 16384 });
    rejects(api.UpdateAppRequest, { version: 1, memory_budget_mb: 16385 });
    rejects(api.UpdateAppRequest, { version: 1, source_path: "/tmp" });
    rejects(api.UpdateAppRequest, { version: 1, display_name: " " });
  });

  it("an owner change sends the version it read", () => {
    accepts(api.SetOwnerRequest, { version: 3, owner: "carol@example.com" });
    rejects(api.SetOwnerRequest, { owner: "carol@example.com" }, "no version");
    rejects(api.SetOwnerRequest, { version: 3, owner: "Carol@example.com" });
  });
});
