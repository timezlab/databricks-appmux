import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { RegisterAppRequest } from "@/lib/api";
import { SLUG_PATTERN } from "@/lib/api/schemas/primitives";
import { appLink, isValidSlug } from "@/lib/slug-link";

const MAX_SLUG_LENGTH = 31;

const VALID = [
  "sales",
  "a",
  "0",
  "9lives",
  "my-app",
  "a-b-c",
  "trailing-",
  "x".repeat(MAX_SLUG_LENGTH),
];

const INVALID = [
  "",
  " ",
  "Sales",
  "SALES",
  "-sales",
  "_appmux",
  "my_app",
  "my app",
  "sales ",
  " sales",
  "sales\n",
  "sales\0",
  "x".repeat(MAX_SLUG_LENGTH + 1),
  "é",
  "sâles",
  "sales.example",
  // Path traversal and path structure.
  ".",
  "..",
  "../",
  "../admin",
  "..%2f",
  "%2e%2e",
  "%2e%2e%2fadmin",
  "a/b",
  "/sales",
  "sales/",
  "/sales/",
  "//evil.example",
  "\\sales",
  "sales\\..\\x",
  // URL structure and schemes.
  "sales?x=1",
  "sales#top",
  "sales@evil.example",
  "javascript:alert(1)",
  "data:text/html,x",
  "https://evil.example/",
  "//",
  // Markup.
  '"><script>alert(1)</script>',
  "sales'onmouseover='x",
];

describe("isValidSlug (the slug pattern of admin-api.md)", () => {
  for (const slug of VALID) {
    it(`accepts ${JSON.stringify(slug)}`, () => {
      assert.equal(isValidSlug(slug), true);
    });
  }

  for (const slug of INVALID) {
    it(`refuses ${JSON.stringify(slug)}`, () => {
      assert.equal(isValidSlug(slug), false);
    });
  }

  it("agrees with the slug schema of the API client on every example above", () => {
    // Both read SLUG_PATTERN from the schema primitives; this keeps the wrapper
    // honest (the string check around the pattern) against the schema itself.
    const slugSchema = RegisterAppRequest.shape.slug;
    for (const slug of [...VALID, ...INVALID]) {
      assert.equal(
        isValidSlug(slug),
        slugSchema.safeParse(slug).success,
        JSON.stringify(slug),
      );
    }
  });

  it("is the exported slug pattern and nothing else", () => {
    for (const slug of [...VALID, ...INVALID]) {
      assert.equal(
        isValidSlug(slug),
        SLUG_PATTERN.test(slug),
        JSON.stringify(slug),
      );
    }
  });
});

describe("appLink", () => {
  it("builds /<slug>/ from a valid slug, outside the admin UI's base path", () => {
    assert.equal(appLink("sales"), "/sales/");
    assert.equal(appLink("my-app"), "/my-app/");
    assert.equal(appLink("0"), "/0/");
  });

  it("returns null for anything that is not a slug, so the caller shows text instead of a link", () => {
    for (const slug of INVALID) {
      assert.equal(appLink(slug), null, JSON.stringify(slug));
    }
  });

  it("never returns a link that leaves the origin or climbs out of the app's prefix", () => {
    for (const slug of [...VALID, ...INVALID]) {
      const link = appLink(slug);
      if (link === null) continue;
      assert.match(link, /^\/[a-z0-9][a-z0-9-]*\/$/);
      assert.ok(!link.startsWith("//"));
      assert.ok(!link.includes(".."));
    }
  });

  it("refuses a slug the type system cannot see is wrong", () => {
    // Values come from JSON; a number or null where a string is expected.
    assert.equal(appLink(undefined as unknown as string), null);
    assert.equal(appLink(null as unknown as string), null);
    assert.equal(appLink(42 as unknown as string), null);
  });
});
