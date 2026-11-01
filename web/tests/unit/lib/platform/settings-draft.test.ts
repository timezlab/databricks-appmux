import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { PlatformView, SettingKey } from "@/lib/api/schemas";
import {
  INITIAL_SETTINGS_DRAFT,
  changedSettingKeys,
  effectiveSettings,
  planSettingsSave,
  runSettingsSave,
  saveSummary,
  settingErrors,
  settingsDraftReducer,
} from "@/lib/platform/settings-draft";
import type {
  SettingDrafts,
  SettingsDraftState,
} from "@/lib/platform/settings-draft";
import { ApiError } from "@/lib/api";

const STORED: PlatformView["settings"] = {
  memory_reserve_mb: 1024,
  package_mode: "wheelhouse",
  default_wheel_sources: ["/Workspace/Shared/appmux/wheelhouse"],
  shared_sp_delegation_allowed: false,
  max_releases_per_app: 10,
  max_locked_releases: 3,
  request_timeout_s: 120,
  max_request_body_mb: 100,
  max_concurrent_requests: 64,
  build_timeout_s: 600,
  max_processes_per_child: 256,
  child_cpu_pin: true,
  attach_check_warehouse_id: null,
};

function edited(
  state: SettingsDraftState,
  key: SettingKey,
  text: string,
  stored = STORED,
): SettingsDraftState {
  return settingsDraftReducer(state, { type: "edited", key, text, stored });
}

describe("changedSettingKeys", () => {
  it("lists only the drafts that differ from the stored value, in the order of the page", () => {
    const drafts: SettingDrafts = {
      max_locked_releases: "2",
      request_timeout_s: "120",
      memory_reserve_mb: "2048",
      build_timeout_s: "abc",
    };
    assert.deepEqual(changedSettingKeys(drafts, STORED), [
      "build_timeout_s",
      "memory_reserve_mb",
      "max_locked_releases",
    ]);
  });

  it("counts text that is not valid yet as a change", () => {
    assert.deepEqual(changedSettingKeys({ build_timeout_s: "" }, STORED), [
      "build_timeout_s",
    ]);
  });
});

describe("effectiveSettings", () => {
  it("lays the valid drafts over the stored values and ignores the invalid ones", () => {
    const effective = effectiveSettings(
      { max_releases_per_app: "20", build_timeout_s: "abc" },
      STORED,
    );
    assert.equal(effective.max_releases_per_app, 20);
    assert.equal(effective.build_timeout_s, 600);
  });
});

describe("settingErrors", () => {
  it("is empty while every changed setting is valid", () => {
    assert.deepEqual(
      settingErrors(
        { request_timeout_s: "200", memory_reserve_mb: "512" },
        STORED,
      ),
      {},
    );
  });

  it("reports the 290 s ceiling and a typo on the rows that have them", () => {
    const errors = settingErrors(
      { request_timeout_s: "300", build_timeout_s: "12x" },
      STORED,
    );
    assert.match(errors.request_timeout_s ?? "", /at most 290/);
    assert.match(errors.build_timeout_s ?? "", /whole number/);
  });

  it("checks the linked pair against the values the person is entering, not the stored ones", () => {
    // Stored 10 and 3. Raising the cap to 20 and the locks to 12 is valid together.
    assert.deepEqual(
      settingErrors(
        { max_releases_per_app: "20", max_locked_releases: "12" },
        STORED,
      ),
      {},
    );
    // Locks of 12 alone are not valid: the stored cap is 10.
    assert.match(
      settingErrors({ max_locked_releases: "12" }, STORED)
        .max_locked_releases ?? "",
      /at most 6/,
    );
  });

  it("flags both rows when a pair of changes breaks the rule between them", () => {
    const errors = settingErrors(
      { max_releases_per_app: "8", max_locked_releases: "5" },
      STORED,
    );
    assert.match(errors.max_locked_releases ?? "", /at most 4/);
    assert.match(errors.max_releases_per_app ?? "", /at least 9/);
  });
});

describe("planSettingsSave", () => {
  it("refuses to send anything while a row is invalid, and says which", () => {
    const plan = planSettingsSave(
      { request_timeout_s: "300", memory_reserve_mb: "512" },
      STORED,
    );
    assert.equal(plan.ok, false);
    if (!plan.ok)
      assert.deepEqual(Object.keys(plan.errors), ["request_timeout_s"]);
  });

  it("builds one PUT body per changed key, in the order of the page", () => {
    const plan = planSettingsSave(
      {
        memory_reserve_mb: "512",
        request_timeout_s: "200",
        child_cpu_pin: "false",
        default_wheel_sources: "/a\n\n/b ",
      },
      STORED,
    );
    assert.equal(plan.ok, true);
    if (plan.ok) {
      assert.deepEqual(
        plan.steps.map((step) => [step.key, step.body.value]),
        [
          ["request_timeout_s", 200],
          ["default_wheel_sources", ["/a", "/b"]],
          ["memory_reserve_mb", 512],
          ["child_cpu_pin", false],
        ],
      );
    }
  });

  it("raises the cap before the locks, so every step is valid on its own", () => {
    const plan = planSettingsSave(
      { max_releases_per_app: "20", max_locked_releases: "12" },
      STORED,
    );
    assert.equal(plan.ok, true);
    if (plan.ok) {
      assert.deepEqual(
        plan.steps.map((step) => step.key),
        ["max_releases_per_app", "max_locked_releases"],
      );
    }
  });

  it("lowers the locks before the cap, so every step is valid on its own", () => {
    const plan = planSettingsSave(
      { max_releases_per_app: "6", max_locked_releases: "2" },
      STORED,
    );
    assert.equal(plan.ok, true);
    if (plan.ok) {
      assert.deepEqual(
        plan.steps.map((step) => step.key),
        ["max_locked_releases", "max_releases_per_app"],
      );
    }
  });

  it("plans nothing when nothing changed", () => {
    const plan = planSettingsSave({ request_timeout_s: "120" }, STORED);
    assert.deepEqual(plan, { ok: true, steps: [] });
  });
});

describe("runSettingsSave", () => {
  function stepsFor(drafts: SettingDrafts) {
    const plan = planSettingsSave(drafts, STORED);
    assert.equal(plan.ok, true);
    return plan.ok ? plan.steps : [];
  }

  it("sends the steps one after the other and reports each", async () => {
    const calls: string[] = [];
    let inFlight = 0;
    let mostInFlight = 0;
    const results = await runSettingsSave(
      stepsFor({ request_timeout_s: "200", memory_reserve_mb: "512" }),
      async (key) => {
        inFlight += 1;
        mostInFlight = Math.max(mostInFlight, inFlight);
        await Promise.resolve();
        calls.push(key);
        inFlight -= 1;
      },
    );
    assert.deepEqual(calls, ["request_timeout_s", "memory_reserve_mb"]);
    assert.equal(mostInFlight, 1);
    assert.deepEqual(
      results.map((result) => [result.key, result.status]),
      [
        ["request_timeout_s", "saved"],
        ["memory_reserve_mb", "saved"],
      ],
    );
  });

  it("keeps going after one row fails, and keeps that row's error", async () => {
    const stale = new ApiError(409, "conflict", "changed by someone else");
    const results = await runSettingsSave(
      stepsFor({
        request_timeout_s: "200",
        memory_reserve_mb: "512",
        child_cpu_pin: "false",
      }),
      (key) =>
        key === "memory_reserve_mb" ? Promise.reject(stale) : Promise.resolve(),
    );
    assert.deepEqual(
      results.map((result) => [result.key, result.status]),
      [
        ["request_timeout_s", "saved"],
        ["memory_reserve_mb", "failed"],
        ["child_cpu_pin", "saved"],
      ],
    );
    const failed = results.find((result) => result.status === "failed");
    assert.ok(failed?.status === "failed");
    assert.equal(failed.error, stale);
  });

  it("does not send the second of the linked pair when the first one failed", async () => {
    const sent: string[] = [];
    const results = await runSettingsSave(
      stepsFor({ max_releases_per_app: "20", max_locked_releases: "12" }),
      (key) => {
        sent.push(key);
        return Promise.reject(new ApiError(400, "invalid", "refused"));
      },
    );
    assert.deepEqual(sent, ["max_releases_per_app"]);
    assert.deepEqual(
      results.map((result) => [result.key, result.status]),
      [
        ["max_releases_per_app", "failed"],
        ["max_locked_releases", "skipped"],
      ],
    );
  });

  it("sends the second of the linked pair after the first one was saved", async () => {
    const sent: string[] = [];
    await runSettingsSave(
      stepsFor({ max_releases_per_app: "20", max_locked_releases: "12" }),
      (key) => {
        sent.push(key);
        return Promise.resolve();
      },
    );
    assert.deepEqual(sent, ["max_releases_per_app", "max_locked_releases"]);
  });
});

describe("saveSummary", () => {
  it("says how many were saved and how many were not", () => {
    const saved = { status: "saved" } as const;
    assert.equal(
      saveSummary([
        { key: "child_cpu_pin", ...saved },
        { key: "build_timeout_s", ...saved },
        { key: "memory_reserve_mb", status: "failed", error: new Error("x") },
      ]),
      "2 of 3 saved, 1 failed",
    );
    assert.equal(
      saveSummary([
        { key: "child_cpu_pin", ...saved },
        {
          key: "build_timeout_s",
          status: "skipped",
          because: "memory_reserve_mb",
        },
      ]),
      "1 of 2 saved, 1 not sent",
    );
    assert.equal(
      saveSummary([{ key: "child_cpu_pin", ...saved }]),
      "Saved 1 setting",
    );
    assert.equal(
      saveSummary([
        { key: "child_cpu_pin", ...saved },
        { key: "build_timeout_s", ...saved },
      ]),
      "Saved 2 settings",
    );
  });
});

describe("settingsDraftReducer", () => {
  it("keeps a draft only while it differs from the stored value", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    assert.deepEqual(state.drafts, { request_timeout_s: "200" });
    assert.equal(state.bases.request_timeout_s, "120");
    state = edited(state, "request_timeout_s", "120");
    assert.deepEqual(state.drafts, {});
    assert.deepEqual(state.bases, {});
  });

  it("remembers the value an edit started from, however many keystrokes follow", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "2");
    state = edited(state, "request_timeout_s", "20");
    state = edited(state, "request_timeout_s", "200");
    assert.equal(state.bases.request_timeout_s, "120");
  });

  it("discards every draft, failure and note, and nothing else", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    state = settingsDraftReducer(state, {
      type: "saveFinished",
      results: [
        { key: "request_timeout_s", status: "failed", error: new Error("x") },
      ],
      saved: {},
      failures: { request_timeout_s: "Could not reach appmux" },
    });
    state = settingsDraftReducer(state, { type: "discarded" });
    assert.deepEqual(state.drafts, {});
    assert.deepEqual(state.failures, {});
    assert.equal(state.lastSave, null);
  });

  it("clears a row's failure when the person edits that row again", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    state = settingsDraftReducer(state, {
      type: "saveFinished",
      results: [
        { key: "request_timeout_s", status: "failed", error: new Error("x") },
      ],
      saved: {},
      failures: { request_timeout_s: "Could not reach appmux" },
    });
    assert.equal(state.failures.request_timeout_s, "Could not reach appmux");
    state = edited(state, "request_timeout_s", "201");
    assert.equal(state.failures.request_timeout_s, undefined);
    assert.equal(state.lastSave, null);
  });

  it("drops the saved rows, keeps the failed ones changed, and records what the server accepted", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    state = edited(state, "memory_reserve_mb", "512", STORED);
    state = settingsDraftReducer(state, {
      type: "saveFinished",
      results: [
        { key: "request_timeout_s", status: "saved" },
        { key: "memory_reserve_mb", status: "failed", error: new Error("x") },
      ],
      saved: { request_timeout_s: 200 },
      failures: { memory_reserve_mb: "The change was not accepted" },
    });
    assert.deepEqual(state.drafts, { memory_reserve_mb: "512" });
    assert.deepEqual(state.confirmed, { request_timeout_s: 200 });
    assert.deepEqual(state.failures, {
      memory_reserve_mb: "The change was not accepted",
    });
    assert.equal(state.lastSave, "1 of 2 saved, 1 failed");
  });

  it("forgets what the server accepted once the next view arrives", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    state = settingsDraftReducer(state, {
      type: "saveFinished",
      results: [{ key: "request_timeout_s", status: "saved" }],
      saved: { request_timeout_s: 200 },
      failures: {},
    });
    state = settingsDraftReducer(state, {
      type: "viewChanged",
      stored: { ...STORED, request_timeout_s: 200 },
    });
    assert.deepEqual(state.confirmed, {});
  });

  it("notes when another admin changed a setting the person is editing, and keeps the draft", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    state = settingsDraftReducer(state, {
      type: "viewChanged",
      stored: { ...STORED, request_timeout_s: 150 },
    });
    assert.deepEqual(state.drafts, { request_timeout_s: "200" });
    assert.equal(state.conflicts.request_timeout_s, "150");
  });

  it("drops a draft that the new stored value now matches", () => {
    let state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    state = settingsDraftReducer(state, {
      type: "viewChanged",
      stored: { ...STORED, request_timeout_s: 200 },
    });
    assert.deepEqual(state.drafts, {});
    assert.deepEqual(state.conflicts, {});
  });

  it("returns the same state when a view brings nothing new", () => {
    const state = edited(INITIAL_SETTINGS_DRAFT, "request_timeout_s", "200");
    assert.equal(
      settingsDraftReducer(state, { type: "viewChanged", stored: STORED }),
      state,
    );
  });
});
