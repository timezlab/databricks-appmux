import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { AppState, ReleaseStatus, RequestStatus } from "@/lib/api";
import {
  appStatePresentation,
  releaseStatusPresentation,
  requestStatusPresentation,
  toneMark,
} from "@/lib/status-presentation";
import type { StatusPresentation, Tone } from "@/lib/status-presentation";

const TONES: readonly Tone[] = [
  "neutral",
  "info",
  "success",
  "warning",
  "danger",
];

function check(
  name: string,
  values: readonly string[],
  presentationOf: (value: never) => StatusPresentation,
) {
  describe(name, () => {
    it("has a presentation for every value of the contract's closed list", () => {
      // The list comes from the schema, so a value added to the contract fails here.
      for (const value of values) {
        const presentation = presentationOf(value as never);
        assert.ok(presentation, `${value} has a presentation`);
        assert.ok(presentation.label.length > 0, `${value} has a label`);
        assert.ok(presentation.mark.length > 0, `${value} has a mark`);
        assert.ok(
          presentation.description.length > 0,
          `${value} has a description`,
        );
        assert.ok(TONES.includes(presentation.tone), `${value} has a tone`);
      }
    });

    it("tells the values apart by their words, so colour is never the only signal", () => {
      const labels = values.map(
        (value) => presentationOf(value as never).label,
      );
      assert.equal(new Set(labels).size, labels.length, labels.join(", "));
    });

    it("gives each tone its own mark, so the shape differs as well as the colour", () => {
      const markByTone = new Map<string, Set<string>>();
      for (const value of values) {
        const { tone, mark } = presentationOf(value as never);
        markByTone.set(tone, (markByTone.get(tone) ?? new Set()).add(mark));
      }
      const marks = [...markByTone.values()].flatMap((set) => [...set]);
      // A mark is used by one tone only: a person who cannot see the colour still gets the shape.
      assert.equal(new Set(marks).size, marks.length);
    });
  });
}

check("release status", ReleaseStatus.options, releaseStatusPresentation);
check("app state", AppState.options, appStatePresentation);
check("request status", RequestStatus.options, requestStatusPresentation);

describe("what each state means to the person", () => {
  it("a release that serves is success, one that failed is danger, one in progress is info", () => {
    assert.equal(releaseStatusPresentation("active").tone, "success");
    assert.equal(releaseStatusPresentation("failed").tone, "danger");
    assert.equal(releaseStatusPresentation("building").tone, "info");
    assert.equal(releaseStatusPresentation("starting").tone, "info");
    assert.equal(releaseStatusPresentation("superseded").tone, "neutral");
  });

  it("says a failed release can still be activated by hand (design 5.1)", () => {
    assert.match(releaseStatusPresentation("failed").description, /by hand/);
  });

  it("an app that is running is success, one that crashed is danger", () => {
    assert.equal(appStatePresentation("running").tone, "success");
    assert.equal(appStatePresentation("crashed").tone, "danger");
    assert.equal(appStatePresentation("stopped").tone, "neutral");
    assert.equal(appStatePresentation("starting").tone, "info");
  });

  it("a request waiting for a decision stands out; a decided one is success or danger", () => {
    assert.equal(requestStatusPresentation("pending").tone, "warning");
    assert.equal(requestStatusPresentation("approved").tone, "success");
    assert.equal(requestStatusPresentation("denied").tone, "danger");
    assert.equal(requestStatusPresentation("withdrawn").tone, "neutral");
  });
});

describe("toneMark (for a page that has a tone but no status of the contract)", () => {
  it("gives every tone a mark, and no two tones the same one, so the shape differs as well as the colour", () => {
    const marks = TONES.map((tone) => toneMark(tone));
    for (const mark of marks) assert.ok(mark.length > 0);
    assert.equal(new Set(marks).size, TONES.length);
  });

  it("is the mark the status badges draw for that tone", () => {
    const presentations = [
      ...ReleaseStatus.options.map((value) => releaseStatusPresentation(value)),
      ...AppState.options.map((value) => appStatePresentation(value)),
      ...RequestStatus.options.map((value) => requestStatusPresentation(value)),
    ];
    for (const presentation of presentations) {
      assert.equal(presentation.mark, toneMark(presentation.tone));
    }
  });
});
