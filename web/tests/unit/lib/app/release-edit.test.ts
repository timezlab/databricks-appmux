import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  RELEASE_NAME_MAX_LENGTH,
  RELEASE_TAGS_MAX,
  RELEASE_TAG_MAX_LENGTH,
  buildReleaseUpdate,
  isReleaseEditChanged,
  parseTags,
  releaseEditDefaults,
} from "@/lib/app/release-edit";
import { readExample } from "../../../support/contract-examples";

const APP = api.AppDetail.parse(readExample("AppDetail"));
const RELEASE = APP.releases[1];
assert.ok(RELEASE);
const NAMED: api.ReleaseSummary = {
  ...RELEASE,
  name: "Q3 launch",
  tags: ["stable", "q3"],
};

describe("releaseEditDefaults", () => {
  it("starts from the release's name and tags", () => {
    assert.deepEqual(releaseEditDefaults(NAMED), {
      name: "Q3 launch",
      tags: "stable, q3",
    });
  });

  it("starts empty for a release with neither", () => {
    assert.deepEqual(releaseEditDefaults({ ...NAMED, name: null, tags: [] }), {
      name: "",
      tags: "",
    });
  });
});

describe("parseTags", () => {
  it("splits on commas and spaces, trims, lower-cases and drops repeats and empties", () => {
    assert.deepEqual(parseTags(" Stable, q3  ,, STABLE\tprod "), [
      "stable",
      "q3",
      "prod",
    ]);
  });

  it("is empty for blank text", () => {
    assert.deepEqual(parseTags("  ,  "), []);
  });
});

describe("buildReleaseUpdate", () => {
  it("builds the whole value of name and tags", () => {
    const result = buildReleaseUpdate({
      name: " Q3 launch ",
      tags: "Stable, q3",
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      name: "Q3 launch",
      tags: ["stable", "q3"],
    });
  });

  it("sends no name as null, which clears it", () => {
    const result = buildReleaseUpdate({ name: "   ", tags: "" });
    assert.ok(result.ok);
    assert.deepEqual(result.body, { name: null, tags: [] });
  });

  it("limits the name at both edges of the contract", () => {
    assert.ok(
      buildReleaseUpdate({
        name: "n".repeat(RELEASE_NAME_MAX_LENGTH),
        tags: "",
      }).ok,
    );
    const result = buildReleaseUpdate({
      name: "n".repeat(RELEASE_NAME_MAX_LENGTH + 1),
      tags: "",
    });
    assert.ok(!result.ok);
    assert.equal(result.errors.name, "At most 80 characters");
  });

  it("limits a tag's length and the number of tags", () => {
    assert.ok(
      buildReleaseUpdate({ name: "", tags: "t".repeat(RELEASE_TAG_MAX_LENGTH) })
        .ok,
    );
    const longTag = buildReleaseUpdate({
      name: "",
      tags: "t".repeat(RELEASE_TAG_MAX_LENGTH + 1),
    });
    assert.ok(!longTag.ok);
    assert.match(longTag.errors.tags ?? "", /each tag/i);

    const tenTags = Array.from({ length: RELEASE_TAGS_MAX }, (_, i) => `t${i}`);
    assert.ok(buildReleaseUpdate({ name: "", tags: tenTags.join(" ") }).ok);
    const tooMany = buildReleaseUpdate({
      name: "",
      tags: [...tenTags, "extra"].join(" "),
    });
    assert.ok(!tooMany.ok);
    assert.match(tooMany.errors.tags ?? "", /at most 10 tags/i);
  });

  it("agrees with the contract's schema on what it builds", () => {
    const result = buildReleaseUpdate({ name: "a", tags: "b c" });
    assert.ok(result.ok);
    assert.ok(api.UpdateReleaseRequest.safeParse(result.body).success);
  });
});

describe("isReleaseEditChanged", () => {
  it("is false for the starting values, however they are spaced or cased", () => {
    assert.equal(
      isReleaseEditChanged(releaseEditDefaults(NAMED), NAMED),
      false,
    );
    assert.equal(
      isReleaseEditChanged({ name: " Q3 launch", tags: "STABLE q3" }, NAMED),
      false,
    );
  });

  it("is true for a different name or tag set", () => {
    assert.equal(
      isReleaseEditChanged({ name: "Q4", tags: "stable, q3" }, NAMED),
      true,
    );
    assert.equal(
      isReleaseEditChanged({ name: "Q3 launch", tags: "stable" }, NAMED),
      true,
    );
    assert.equal(isReleaseEditChanged({ name: "", tags: "" }, NAMED), true);
  });

  it("treats no name and an empty name alike", () => {
    const bare = { ...NAMED, name: null, tags: [] };
    assert.equal(isReleaseEditChanged({ name: "  ", tags: "" }, bare), false);
  });
});
