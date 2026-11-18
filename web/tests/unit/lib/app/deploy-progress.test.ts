import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { deployProgress } from "@/lib/app/deploy-progress";
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
  return { ...EXAMPLE_APP, state: "running", ...overrides };
}

describe("deployProgress", () => {
  it("is null while no release is being built or started", () => {
    assert.equal(
      deployProgress(appWith({ releases: [release(7, { status: "active" })] })),
      null,
    );
    assert.equal(deployProgress(appWith({ releases: [] })), null);
  });

  it("is null for a release that failed: Overview already says why", () => {
    const app = appWith({
      releases: [release(8, { status: "failed", is_target: true })],
    });
    assert.equal(deployProgress(app), null);
  });

  it.each([
    ["pending", "Pending", 1],
    ["building", "Building", 2],
    ["starting", "Starting", 3],
    ["healthy", "Healthy", 4],
  ] as const)(
    "puts a %s release on step %i of the way to active",
    (status, label, index) => {
      const app = appWith({
        releases: [
          release(9, { status, is_target: true }),
          release(7, { status: "active" }),
        ],
      });
      const progress = deployProgress(app);
      assert.ok(progress);
      assert.equal(progress.releaseNo, 9);
      assert.equal(progress.currentLabel, label);
      assert.deepEqual(
        progress.steps.map((step) => step.label),
        [
          "Reading the folder",
          "Pending",
          "Building",
          "Starting",
          "Healthy",
          "Active",
        ],
      );
      assert.deepEqual(
        progress.steps.map((step) => step.state),
        progress.steps.map((_, at) =>
          at < index ? "done" : at === index ? "current" : "todo",
        ),
      );
    },
  );

  it("shows a Deploy still reading its folder, before any release exists", () => {
    const app = appWith({
      releases: [release(7, { status: "active" })],
      deploy_preparing: {
        started_at: "2026-11-18T09:00:00Z",
        started_by: "carol@example.com",
      },
    });
    const progress = deployProgress(app);
    assert.ok(progress);
    assert.equal(progress.releaseNo, null);
    assert.equal(progress.currentLabel, "Reading the folder");
    assert.deepEqual(
      progress.steps.map((step) => step.state),
      ["current", "todo", "todo", "todo", "todo", "todo"],
    );
    assert.equal(progress.servingReleaseNo, 7);
    assert.equal(progress.isBuildOutputRecorded, false);
  });

  it("follows the release once it exists, even while the flow is still answering", () => {
    const app = appWith({
      releases: [
        release(9, { status: "pending", is_target: true }),
        release(7, { status: "active" }),
      ],
      deploy_preparing: {
        started_at: "2026-11-18T09:00:00Z",
        started_by: "carol@example.com",
      },
    });
    const progress = deployProgress(app);
    assert.equal(progress?.releaseNo, 9);
    assert.equal(progress?.currentLabel, "Pending");
  });

  it("says the running release keeps serving until the new one is healthy", () => {
    const app = appWith({
      releases: [
        release(9, { status: "building", is_target: true }),
        release(7, { status: "active" }),
      ],
    });
    const progress = deployProgress(app);
    assert.equal(progress?.servingReleaseNo, 7);
    assert.match(progress?.outlook ?? "", /release 7 keeps serving/i);
  });

  it("says the route opens after the health check when nothing serves yet", () => {
    const app = appWith({
      state: "no_release",
      releases: [release(1, { status: "building", is_target: true })],
    });
    const progress = deployProgress(app);
    assert.equal(progress?.servingReleaseNo, null);
    assert.match(progress?.outlook ?? "", /route opens/i);
  });

  it("does not claim a release serves when the app is not running", () => {
    const app = appWith({
      state: "starting",
      releases: [
        release(9, { status: "building", is_target: true }),
        release(7, { status: "active" }),
      ],
    });
    const progress = deployProgress(app);
    assert.equal(progress?.servingReleaseNo, null);
    assert.doesNotMatch(progress?.outlook ?? "", /keeps serving/i);
  });

  it("notes that build output is not recorded until the copy starts", () => {
    const releases = (status: api.ReleaseStatus) => [
      release(9, { status, is_target: true }),
      release(7, { status: "active" }),
    ];
    for (const status of ["pending", "building"] as const) {
      assert.equal(
        deployProgress(appWith({ releases: releases(status) }))
          ?.isBuildOutputRecorded,
        false,
        status,
      );
    }
    for (const status of ["starting", "healthy"] as const) {
      assert.equal(
        deployProgress(appWith({ releases: releases(status) }))
          ?.isBuildOutputRecorded,
        true,
        status,
      );
    }
  });
});
