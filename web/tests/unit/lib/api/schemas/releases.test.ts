/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { accepts, rejects } from "../../../../support/schema-assertions";

describe("release name and tags", () => {
  it("name: null, or 1 to 80 characters", () => {
    accepts(api.UpdateReleaseRequest, { name: null, tags: [] });
    accepts(api.UpdateReleaseRequest, { name: "a", tags: [] });
    accepts(api.UpdateReleaseRequest, { name: "a".repeat(80), tags: [] });
    rejects(api.UpdateReleaseRequest, { name: "", tags: [] });
    rejects(api.UpdateReleaseRequest, { name: "a".repeat(81), tags: [] });
    rejects(api.UpdateReleaseRequest, { tags: [] }, "name is required");
  });

  it("tags: at most 10, each lower case and at most 32 characters", () => {
    const tags = (count: number) =>
      Array.from({ length: count }, (_, index) => `t${index}`);
    accepts(api.UpdateReleaseRequest, { name: null, tags: tags(10) });
    rejects(api.UpdateReleaseRequest, { name: null, tags: tags(11) });
    accepts(api.UpdateReleaseRequest, { name: null, tags: ["a".repeat(32)] });
    rejects(api.UpdateReleaseRequest, { name: null, tags: ["a".repeat(33)] });
    accepts(api.UpdateReleaseRequest, { name: null, tags: ["prod-2"] });
    rejects(api.UpdateReleaseRequest, { name: null, tags: ["Prod"] });
    rejects(api.UpdateReleaseRequest, { name: null, tags: ["prod", "Beta"] });
  });
});
