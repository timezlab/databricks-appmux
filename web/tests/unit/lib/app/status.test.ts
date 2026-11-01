import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  activeRelease,
  failedTargetRelease,
  inProgressRelease,
  reconcileSelection,
  releaseLockLabel,
  releaseMarkers,
  releasesNewestFirst,
  statusNotices,
} from "@/lib/app/status";
import { readExample } from "../../../support/contract-examples";

const EXAMPLE_APP = api.AppDetail.parse(readExample("AppDetail"));

function release(
  releaseNo: number,
  overrides: Partial<api.ReleaseSummary> = {},
): api.ReleaseSummary {
  const base = EXAMPLE_APP.releases[2];
  assert.ok(base);
  return {
    ...base,
    release_no: releaseNo,
    status: "superseded",
    status_reason: null,
    is_target: false,
    locked_by: null,
    locked_at: null,
    ...overrides,
  };
}

function appWith(overrides: Partial<api.AppDetail>): api.AppDetail {
  return {
    ...EXAMPLE_APP,
    state: "running",
    restart_to_apply: false,
    requirements: { ...EXAMPLE_APP.requirements, items: [] },
    ...overrides,
  };
}

function ids(app: api.AppDetail): string[] {
  return statusNotices(app).map((notice) => notice.id);
}

describe("release roles", () => {
  it("finds the active release, the failed target and the one in progress", () => {
    assert.equal(activeRelease(EXAMPLE_APP)?.release_no, 7);
    assert.equal(failedTargetRelease(EXAMPLE_APP)?.release_no, 8);
    assert.equal(inProgressRelease(EXAMPLE_APP), null);

    const building = appWith({
      releases: [
        release(9, { status: "building", is_target: true }),
        release(7, { status: "active" }),
      ],
    });
    assert.equal(inProgressRelease(building)?.release_no, 9);
    assert.equal(failedTargetRelease(building), null);
  });

  it("finds nothing in an app without releases", () => {
    const empty = appWith({ releases: [] });
    assert.equal(activeRelease(empty), null);
    assert.equal(failedTargetRelease(empty), null);
    assert.equal(inProgressRelease(empty), null);
  });

  it("does not call a failed release that is no longer the target a failed target", () => {
    const app = appWith({
      releases: [
        release(8, { status: "failed", is_target: false }),
        release(7, { status: "active", is_target: true }),
      ],
    });
    assert.equal(failedTargetRelease(app), null);
  });
});

describe("releasesNewestFirst", () => {
  it("orders by release number, newest first, without touching the input", () => {
    const releases = [release(2), release(9), release(5)];
    assert.deepEqual(
      releasesNewestFirst(releases).map((item) => item.release_no),
      [9, 5, 2],
    );
    assert.deepEqual(
      releases.map((item) => item.release_no),
      [2, 9, 5],
    );
  });
});

describe("releaseMarkers and releaseLockLabel", () => {
  it("marks the target that is not yet (or no longer) active", () => {
    assert.deepEqual(releaseMarkers(release(8, { is_target: true })), [
      "target",
    ]);
    assert.deepEqual(
      releaseMarkers(release(7, { status: "active", is_target: true })),
      [],
    );
    assert.deepEqual(releaseMarkers(release(6)), []);
  });

  it("names who locked a release and when it is protected without a mark", () => {
    assert.equal(
      releaseLockLabel(release(5, { locked_by: "alice@example.com" })),
      "Locked by alice@example.com",
    );
    assert.equal(
      releaseLockLabel(release(7, { status: "active" })),
      "Protected while active",
    );
    assert.equal(releaseLockLabel(release(6)), null);
  });
});

describe("statusNotices", () => {
  it("says nothing about a healthy app", () => {
    const app = appWith({
      releases: [release(7, { status: "active", is_target: true })],
    });
    assert.deepEqual(ids(app), []);
  });

  it("reports a crashed app as a danger", () => {
    const notices = statusNotices(
      appWith({
        state: "crashed",
        releases: [release(7, { status: "active", is_target: true })],
      }),
    );
    assert.equal(notices[0]?.id, "crashed");
    assert.equal(notices[0]?.tone, "danger");
  });

  it("asks for a restart when resources changed since the last start", () => {
    const notices = statusNotices(
      appWith({
        restart_to_apply: true,
        releases: [release(7, { status: "active", is_target: true })],
      }),
    );
    assert.deepEqual(
      notices.map((notice) => [notice.id, notice.tone]),
      [["restart-to-apply", "warning"]],
    );
  });

  it("reports a failed target with its reason and says what keeps serving", () => {
    const [notice] = statusNotices(EXAMPLE_APP);
    assert.equal(notice?.id, "failed-target");
    assert.equal(notice?.tone, "warning");
    assert.match(notice?.title ?? "", /Release 8 failed/);
    assert.match(notice?.detail ?? "", /release 7/i);
    const failed = EXAMPLE_APP.releases[0];
    assert.ok(failed?.status_reason);
    assert.ok((notice?.detail ?? "").includes(failed.status_reason));
  });

  it("says no release is serving when the failed target is all there is", () => {
    const app = appWith({
      releases: [release(1, { status: "failed", is_target: true })],
    });
    const notice = statusNotices(app).find(
      (item) => item.id === "failed-target",
    );
    assert.match(notice?.detail ?? "", /no release is serving/i);
  });

  it("tells that the current release keeps serving while a new one is built", () => {
    const app = appWith({
      releases: [
        release(9, { status: "building", is_target: true }),
        release(7, { status: "active" }),
      ],
    });
    const notice = statusNotices(app).find((item) => item.id === "in-progress");
    assert.equal(notice?.tone, "info");
    assert.match(notice?.title ?? "", /Release 9/);
    assert.match(notice?.title ?? "", /building/i);
    assert.match(notice?.detail ?? "", /keeps serving/i);
  });

  it("invites the first Update when there is no release", () => {
    const app = appWith({ releases: [], state: "stopped" });
    assert.deepEqual(ids(app), ["no-release"]);
    assert.match(statusNotices(app)[0]?.detail ?? "", /Update/);
  });

  it("counts the missing requirements", () => {
    const missing = {
      key: "SALES_DB",
      state: "missing",
      offer: null,
      suggested_type: "sql_warehouse",
    } as const;
    const attached = { ...missing, key: "TOKEN", state: "attached" } as const;
    const app = appWith({
      releases: [release(7, { status: "active", is_target: true })],
      requirements: {
        release_no: 7,
        items: [missing, attached],
        unreferenced_offers: [],
      },
    });
    const notice = statusNotices(app).find(
      (item) => item.id === "requirements-missing",
    );
    assert.equal(notice?.tone, "warning");
    assert.match(notice?.title ?? "", /1 requirement is missing/);
  });
});

describe("reconcileSelection", () => {
  const releases = [release(9), release(5)];

  it("keeps a selection that is still in the list", () => {
    assert.equal(reconcileSelection(5, releases), 5);
  });

  it("drops a selection whose release has been deleted", () => {
    assert.equal(reconcileSelection(4, releases), null);
  });

  it("keeps no selection", () => {
    assert.equal(reconcileSelection(null, releases), null);
  });
});
