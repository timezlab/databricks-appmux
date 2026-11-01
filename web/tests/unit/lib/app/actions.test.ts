import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  REASON_NOT_MANAGER,
  REASON_UNKNOWN_ACTOR,
  appActionAvailability,
  canViewSource,
  hasActiveRelease,
  hasManualLock,
  isReleaseInProgress,
  manageAvailability,
  manualLockCount,
  releaseActionAvailability,
  resolveActor,
  shouldReloadAfterError,
} from "@/lib/app/actions";
import type { Actor, Availability } from "@/lib/app/actions";
import { readExample } from "../../../support/contract-examples";

const EXAMPLE_APP = api.AppDetail.parse(readExample("AppDetail"));
const EXAMPLE_ME = api.MeResponse.parse(readExample("MeResponse"));
const EXAMPLE_SUMMARY = api.AppSummary.parse(readExample("AppSummary"));

const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const VISITOR: Actor = {
  email: "bob@example.com",
  isPlatformAdmin: false,
  permission: "CAN_USE",
  canManage: false,
};

function app(overrides: Partial<api.AppDetail> = {}): api.AppDetail {
  return { ...EXAMPLE_APP, ...overrides };
}

function appWithDesiredState(
  desiredState: api.DesiredState,
  state: api.AppState,
  releases: api.ReleaseSummary[] = EXAMPLE_APP.releases,
): api.AppDetail {
  return {
    ...EXAMPLE_APP,
    state,
    releases,
    config: { ...EXAMPLE_APP.config, desired_state: desiredState },
  };
}

function release(
  releaseNo: number,
  overrides: Partial<api.ReleaseSummary> = {},
): api.ReleaseSummary {
  const base = EXAMPLE_APP.releases[2];
  assert.ok(base, "the example holds a plain superseded release");
  return {
    ...base,
    release_no: releaseNo,
    status: "superseded",
    is_target: false,
    locked_by: null,
    locked_at: null,
    ...overrides,
  };
}

function reasonOf(availability: Availability): string {
  assert.equal(availability.isAllowed, false, "expected a refusal");
  return availability.isAllowed ? "" : availability.reason;
}

function assertAllowed(availability: Availability): void {
  assert.deepEqual(availability, { isAllowed: true });
}

describe("resolveActor", () => {
  it("takes the level of this app from the launcher list and the admin flag from /me", () => {
    const actor = resolveActor(EXAMPLE_ME, [EXAMPLE_SUMMARY], "sales");
    assert.deepEqual(actor, {
      email: "dana@example.com",
      isPlatformAdmin: true,
      permission: "CAN_MANAGE",
      canManage: true,
    });
  });

  it("does not manage when the level is CAN_USE", () => {
    const actor = resolveActor(
      { ...EXAMPLE_ME, is_platform_admin: false },
      [{ ...EXAMPLE_SUMMARY, permission: "CAN_USE" }],
      "sales",
    );
    assert.equal(actor?.permission, "CAN_USE");
    assert.equal(actor?.canManage, false);
  });

  it("has no level when the app is not in the list; only a platform admin (implicit CAN_MANAGE) then manages", () => {
    const admin = resolveActor(EXAMPLE_ME, [EXAMPLE_SUMMARY], "wiki");
    assert.equal(admin?.permission, null);
    assert.equal(admin?.canManage, true);

    const visitor = resolveActor(
      { ...EXAMPLE_ME, is_platform_admin: false },
      [EXAMPLE_SUMMARY],
      "wiki",
    );
    assert.equal(visitor?.permission, null);
    assert.equal(visitor?.canManage, false);
  });

  it("is unknown until both answers are in", () => {
    assert.equal(resolveActor(null, [EXAMPLE_SUMMARY], "sales"), null);
    assert.equal(resolveActor(EXAMPLE_ME, null, "sales"), null);
  });
});

describe("release helpers", () => {
  it("a release is in progress from pending to healthy", () => {
    for (const status of [
      "pending",
      "building",
      "starting",
      "healthy",
    ] as const) {
      assert.equal(isReleaseInProgress(status), true, status);
    }
    for (const status of ["active", "failed", "superseded"] as const) {
      assert.equal(isReleaseInProgress(status), false, status);
    }
  });

  it("only a hand lock is a lock; the active release carries none", () => {
    assert.equal(
      hasManualLock(release(5, { locked_by: "a@example.com" })),
      true,
    );
    assert.equal(hasManualLock(release(7, { status: "active" })), false);
  });

  it("counts the hand locks of an app", () => {
    assert.equal(manualLockCount(EXAMPLE_APP), 1);
    assert.equal(manualLockCount(app({ releases: [] })), 0);
  });

  it("finds the active release", () => {
    assert.equal(hasActiveRelease(EXAMPLE_APP), true);
    assert.equal(hasActiveRelease(app({ releases: [release(1)] })), false);
  });
});

describe("appActionAvailability", () => {
  const ACTIONS = ["update", "restart", "stop", "start"] as const;

  it("is refused to someone who does not manage the app", () => {
    for (const action of ACTIONS) {
      assert.equal(
        reasonOf(appActionAvailability(action, EXAMPLE_APP, VISITOR)),
        REASON_NOT_MANAGER,
        action,
      );
    }
  });

  it("waits while the actor is not known", () => {
    for (const action of ACTIONS) {
      assert.equal(
        reasonOf(appActionAvailability(action, EXAMPLE_APP, null)),
        REASON_UNKNOWN_ACTOR,
        action,
      );
    }
  });

  it("allows Update to a manager in every state", () => {
    for (const state of [
      "stopped",
      "starting",
      "running",
      "crashed",
    ] as const) {
      assertAllowed(
        appActionAvailability(
          "update",
          appWithDesiredState("running", state),
          MANAGER,
        ),
      );
    }
    assertAllowed(
      appActionAvailability("update", app({ releases: [] }), MANAGER),
    );
  });

  it("allows Stop and Restart while the app is meant to run, Start only when it is meant to be stopped", () => {
    const running = appWithDesiredState("running", "running");
    assertAllowed(appActionAvailability("stop", running, MANAGER));
    assertAllowed(appActionAvailability("restart", running, MANAGER));
    assert.match(
      reasonOf(appActionAvailability("start", running, MANAGER)),
      /already/i,
    );

    const stopped = appWithDesiredState("stopped", "stopped");
    assertAllowed(appActionAvailability("start", stopped, MANAGER));
    assert.match(
      reasonOf(appActionAvailability("stop", stopped, MANAGER)),
      /already stopped/i,
    );
    assert.match(
      reasonOf(appActionAvailability("restart", stopped, MANAGER)),
      /Start/,
    );
  });

  it("lets a crashed app be restarted or stopped, since it is meant to run", () => {
    const crashed = appWithDesiredState("running", "crashed");
    assertAllowed(appActionAvailability("restart", crashed, MANAGER));
    assertAllowed(appActionAvailability("stop", crashed, MANAGER));
  });

  it("cannot start or restart an app that has no active release, and points to Update", () => {
    const noRelease = appWithDesiredState("stopped", "stopped", []);
    assert.match(
      reasonOf(appActionAvailability("start", noRelease, MANAGER)),
      /Update/,
    );
    const failedOnly = appWithDesiredState("running", "stopped", [
      release(1, { status: "failed", is_target: true }),
    ]);
    assert.match(
      reasonOf(appActionAvailability("restart", failedOnly, MANAGER)),
      /Update/,
    );
  });
});

describe("releaseActionAvailability", () => {
  function reason(
    action: Parameters<typeof releaseActionAvailability>[0],
    candidate: api.ReleaseSummary,
    actor: Actor | null = MANAGER,
  ): string {
    return reasonOf(
      releaseActionAvailability(action, candidate, EXAMPLE_APP, actor),
    );
  }
  function allowed(
    action: Parameters<typeof releaseActionAvailability>[0],
    candidate: api.ReleaseSummary,
  ): void {
    assertAllowed(
      releaseActionAvailability(action, candidate, EXAMPLE_APP, MANAGER),
    );
  }

  it("is refused to someone who does not manage the app, and waits for an unknown actor", () => {
    for (const action of [
      "activate",
      "lock",
      "unlock",
      "delete",
      "edit",
    ] as const) {
      assert.equal(
        reason(action, release(5, { locked_by: "a@example.com" }), VISITOR),
        REASON_NOT_MANAGER,
      );
      assert.equal(
        reason(action, release(5, { locked_by: "a@example.com" }), null),
        REASON_UNKNOWN_ACTOR,
      );
    }
  });

  describe("activate (any live release, a failed one included)", () => {
    it("allows a superseded and a failed release", () => {
      allowed("activate", release(6));
      allowed("activate", release(8, { status: "failed", is_target: true }));
    });

    it("refuses the release that is already active and the target", () => {
      assert.match(
        reason("activate", release(7, { status: "active", is_target: true })),
        /already active/i,
      );
    });

    it("allows the active release when a failed target has taken its place as target", () => {
      allowed("activate", release(7, { status: "active", is_target: false }));
    });

    it("refuses a release that is still being built or started", () => {
      for (const status of [
        "pending",
        "building",
        "starting",
        "healthy",
      ] as const) {
        assert.match(
          reason("activate", release(9, { status, is_target: true })),
          /on its way|being/i,
          status,
        );
      }
    });
  });

  describe("lock", () => {
    it("allows any live release that is not locked by hand, the active one included", () => {
      allowed("lock", release(6));
      allowed("lock", release(7, { status: "active" }));
      allowed("lock", release(8, { status: "failed", is_target: true }));
    });

    // Design 5.2: "An app admin may lock any live release by hand"; 9.2 and the
    // contract refuse a lock only at max_locked_releases (the server's 409).
    it("allows a release that is still being built or started, as the design does", () => {
      for (const status of [
        "pending",
        "building",
        "starting",
        "healthy",
      ] as const) {
        allowed("lock", release(9, { status, is_target: true }));
      }
    });

    it("refuses one that is already locked, and names who locked it", () => {
      assert.match(
        reason("lock", release(5, { locked_by: "alice@example.com" })),
        /alice@example\.com/,
      );
    });
  });

  describe("unlock", () => {
    it("allows a release locked by hand, to any app admin", () => {
      allowed("unlock", release(5, { locked_by: "carol@example.com" }));
    });

    it("has nothing to do on a release that is not locked by hand", () => {
      assert.match(reason("unlock", release(6)), /not locked/i);
    });

    it("says the active release is locked without a mark, so there is nothing to unlock", () => {
      assert.match(
        reason("unlock", release(7, { status: "active" })),
        /active/i,
      );
    });
  });

  describe("edit (name and tags)", () => {
    it("is allowed on any release, whatever its status or lock", () => {
      allowed("edit", release(5, { status: "active" }));
      allowed("edit", release(6, { status: "building" }));
      allowed("edit", release(7, { locked_by: "carol@example.com" }));
    });
  });

  describe("delete (not the active, target, in-progress or locked release)", () => {
    it("allows a superseded release with no lock", () => {
      allowed("delete", release(6));
      allowed("delete", release(4, { status: "failed" }));
    });

    it("refuses the active release", () => {
      assert.match(
        reason("delete", release(7, { status: "active" })),
        /active/i,
      );
    });

    it("refuses the target, even a failed one, and says to activate another first", () => {
      const text = reason(
        "delete",
        release(8, { status: "failed", is_target: true }),
      );
      assert.match(text, /target/i);
      assert.match(text, /activate/i);
    });

    it("refuses a release being built or started", () => {
      for (const status of [
        "pending",
        "building",
        "starting",
        "healthy",
      ] as const) {
        assert.match(
          reason("delete", release(9, { status })),
          /built or started|progress/i,
          status,
        );
      }
    });

    it("refuses a locked release and says to unlock it first", () => {
      assert.match(
        reason("delete", release(5, { locked_by: "alice@example.com" })),
        /unlock/i,
      );
    });

    it("reports the strongest protection first (active before locked)", () => {
      assert.match(
        reason(
          "delete",
          release(7, { status: "active", locked_by: "a@example.com" }),
        ),
        /active/i,
      );
    });
  });

  it("agrees with the contract's example list", () => {
    const [failedTarget, active, superseded, locked] = EXAMPLE_APP.releases;
    assert.ok(failedTarget && active && superseded && locked);
    assert.equal(
      releaseActionAvailability("delete", failedTarget, EXAMPLE_APP, MANAGER)
        .isAllowed,
      false,
    );
    assert.equal(
      releaseActionAvailability("delete", active, EXAMPLE_APP, MANAGER)
        .isAllowed,
      false,
    );
    assert.equal(
      releaseActionAvailability("delete", superseded, EXAMPLE_APP, MANAGER)
        .isAllowed,
      true,
    );
    assert.equal(
      releaseActionAvailability("delete", locked, EXAMPLE_APP, MANAGER)
        .isAllowed,
      false,
    );
    assert.equal(
      releaseActionAvailability("unlock", locked, EXAMPLE_APP, MANAGER)
        .isAllowed,
      true,
    );
    assert.equal(
      releaseActionAvailability("activate", active, EXAMPLE_APP, MANAGER)
        .isAllowed,
      true,
    );
  });
});

describe("manageAvailability", () => {
  it("is on for a manager, off with its reason for a visitor, and waits for an unknown actor", () => {
    assertAllowed(manageAvailability(MANAGER));
    assert.equal(reasonOf(manageAvailability(VISITOR)), REASON_NOT_MANAGER);
    assert.equal(reasonOf(manageAvailability(null)), REASON_UNKNOWN_ACTOR);
  });
});

describe("canViewSource", () => {
  it("is for a manager of the app, which a platform admin always is", () => {
    assertAllowed(canViewSource(MANAGER));
    assertAllowed(
      canViewSource({
        ...MANAGER,
        isPlatformAdmin: true,
        email: "dana@example.com",
      }),
    );
  });

  it("is refused to a visitor, and waits for an unknown actor", () => {
    assert.equal(reasonOf(canViewSource(VISITOR)), REASON_NOT_MANAGER);
    assert.equal(reasonOf(canViewSource(null)), REASON_UNKNOWN_ACTOR);
  });
});

describe("shouldReloadAfterError", () => {
  it("reloads after a stale version or a state that changed (409) and after a vanished release (404)", () => {
    assert.equal(
      shouldReloadAfterError(
        new api.ApiError(
          409,
          "conflict",
          "changed by someone else, reload and retry",
        ),
      ),
      true,
    );
    assert.equal(
      shouldReloadAfterError(
        new api.ApiError(404, "not_found", "no such release"),
      ),
      true,
    );
  });

  it("does not reload after a refusal, a lost connection or a bug", () => {
    assert.equal(
      shouldReloadAfterError(new api.ApiError(400, "bad", "x")),
      false,
    );
    assert.equal(
      shouldReloadAfterError(new api.ApiError(403, "forbidden", "x")),
      false,
    );
    assert.equal(
      shouldReloadAfterError(new api.ApiError(0, "network_error", "x")),
      false,
    );
    assert.equal(shouldReloadAfterError(new Error("boom")), false);
    assert.equal(shouldReloadAfterError("boom"), false);
  });
});
