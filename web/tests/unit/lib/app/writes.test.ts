import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  RELOAD_NOTICE_TEXT,
  WriteBusyError,
  createWriteController,
  writeStateOf,
} from "@/lib/app/writes";
import type { WriteNotice } from "@/lib/app/writes";

/** A promise the test settles by hand, so two writes can overlap. */
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

/** A controller whose three effects are recorded in the order they happen. */
function harness() {
  const events: string[] = [];
  const notices: (WriteNotice | null)[] = [];
  const busyKeys: (string | null)[] = [];
  const controller = createWriteController({
    reload: () => {
      events.push("reload");
    },
    setNotice: (notice) => {
      notices.push(notice);
      events.push(notice === null ? "notice:cleared" : `notice:${notice.tone}`);
    },
    setBusyKey: (key) => {
      busyKeys.push(key);
      events.push(`busy:${key ?? "none"}`);
    },
  });
  const reloadCount = () => events.filter((event) => event === "reload").length;
  return { controller, events, notices, busyKeys, reloadCount };
}

const STALE = new api.ApiError(
  409,
  "changed_by_others",
  "changed by someone else, reload and retry",
);

describe("writeStateOf", () => {
  it("is idle when nothing runs, busy for the running write and blocked for the rest", () => {
    assert.equal(writeStateOf(null, "update"), "idle");
    assert.equal(writeStateOf("update", "update"), "busy");
    assert.equal(writeStateOf("update", "stop"), "blocked");
  });
});

describe("createWriteController: a successful write", () => {
  it("says what happened, reads the app again once and returns the value", async () => {
    const { controller, events, reloadCount } = harness();
    const result = await controller.run(
      "update",
      () => Promise.resolve(7),
      "Release 7 was created.",
    );
    assert.deepEqual(result, { ok: true, value: 7 });
    assert.equal(reloadCount(), 1);
    assert.deepEqual(events, [
      "busy:update",
      "notice:cleared",
      "notice:success",
      "reload",
      "busy:none",
    ]);
  });

  it("shows the sentence for the value the server answered", async () => {
    const { controller, notices } = harness();
    await controller.run(
      "update",
      () => Promise.resolve({ releaseNo: 9 }),
      (value) => `Release ${value.releaseNo} was created.`,
    );
    assert.deepEqual(notices.at(-1), {
      tone: "success",
      text: "Release 9 was created.",
    });
  });

  it("clears the sentence of the previous write before it starts", async () => {
    const { controller, notices } = harness();
    await controller.run("a", () => Promise.resolve(1), "First.");
    await controller.run("b", () => Promise.resolve(2), "Second.");
    assert.deepEqual(notices, [
      null,
      { tone: "success", text: "First." },
      null,
      { tone: "success", text: "Second." },
    ]);
  });
});

describe("createWriteController: one write at a time", () => {
  it("refuses a second write while the first runs, without calling it or reading again", async () => {
    const { controller, busyKeys, reloadCount, notices } = harness();
    const first = deferred<string>();
    const running = controller.run("update", () => first.promise, "Done.");

    let secondCalls = 0;
    const second = await controller.run(
      "stop",
      () => {
        secondCalls += 1;
        return Promise.resolve("never");
      },
      "Stopped.",
    );

    assert.ok(!second.ok);
    assert.ok(second.error instanceof WriteBusyError);
    assert.equal(secondCalls, 0);
    assert.equal(reloadCount(), 0);
    // The refusal touches neither the busy key of the running write nor its notice.
    assert.deepEqual(busyKeys, ["update"]);
    assert.deepEqual(notices, [null]);

    first.resolve("ok");
    assert.deepEqual(await running, { ok: true, value: "ok" });
    assert.equal(reloadCount(), 1);
    assert.deepEqual(busyKeys, ["update", null]);
  });

  it("refuses the same action pressed twice before the first answer", async () => {
    const { controller } = harness();
    const first = deferred<number>();
    const running = controller.run("lock:5", () => first.promise, "Locked.");
    const again = await controller.run(
      "lock:5",
      () => Promise.resolve(1),
      "Locked.",
    );
    assert.ok(!again.ok);
    assert.ok(again.error instanceof WriteBusyError);
    first.resolve(1);
    await running;
  });

  it("accepts the next write once the previous one succeeded", async () => {
    const { controller } = harness();
    await controller.run("a", () => Promise.resolve(1), "A.");
    const next = await controller.run("b", () => Promise.resolve(2), "B.");
    assert.deepEqual(next, { ok: true, value: 2 });
  });

  it("accepts the next write once the previous one failed", async () => {
    const { controller } = harness();
    const failed = await controller.run(
      "a",
      () => Promise.reject(new api.ApiError(400, "bad", "no")),
      "A.",
    );
    assert.ok(!failed.ok);
    const next = await controller.run("b", () => Promise.resolve(2), "B.");
    assert.deepEqual(next, { ok: true, value: 2 });
  });
});

describe("createWriteController: replacing the effects", () => {
  it("calls the latest effects while keeping the one-write-at-a-time guard", async () => {
    const first = harness();
    const second: string[] = [];
    const slow = deferred<number>();
    const running = first.controller.run("update", () => slow.promise, "Done.");

    // The page re-rendered while the write was in flight and passed new callbacks.
    first.controller.setEffects({
      reload: () => {
        second.push("reload");
      },
      setNotice: (notice) => {
        second.push(`notice:${notice?.tone ?? "cleared"}`);
      },
      setBusyKey: (key) => {
        second.push(`busy:${key ?? "none"}`);
      },
    });
    const refused = await first.controller.run(
      "stop",
      () => Promise.resolve(1),
      "Stopped.",
    );
    assert.ok(!refused.ok);
    assert.ok(refused.error instanceof WriteBusyError);

    slow.resolve(1);
    await running;
    assert.deepEqual(second, ["notice:success", "reload", "busy:none"]);
    assert.equal(first.reloadCount(), 0);
  });
});

describe("createWriteController: a failed write", () => {
  it("reads the app again exactly once after a 409, says so, and hands the error back", async () => {
    const { controller, events, notices, reloadCount } = harness();
    const result = await controller.run(
      "activate:5",
      () => Promise.reject(STALE),
      "Activated.",
    );
    assert.deepEqual(result, { ok: false, error: STALE });
    assert.equal(reloadCount(), 1);
    assert.deepEqual(notices.at(-1), {
      tone: "info",
      text: RELOAD_NOTICE_TEXT,
    });
    assert.deepEqual(events, [
      "busy:activate:5",
      "notice:cleared",
      "reload",
      "notice:info",
      "busy:none",
    ]);
  });

  it("does the same after a 404, the release or the app being gone", async () => {
    const { controller, notices, reloadCount } = harness();
    const gone = new api.ApiError(404, "not_found", "no such release");
    const result = await controller.run(
      "delete:5",
      () => Promise.reject(gone),
      "Deleted.",
    );
    assert.deepEqual(result, { ok: false, error: gone });
    assert.equal(reloadCount(), 1);
    assert.deepEqual(notices.at(-1), {
      tone: "info",
      text: RELOAD_NOTICE_TEXT,
    });
  });

  it("does not read again, or announce a reload, after a refusal, a lost connection or a bug", async () => {
    const errors: unknown[] = [
      new api.ApiError(400, "bad_request", "no"),
      new api.ApiError(403, "forbidden", "no"),
      new api.ApiError(409, "slug_taken", "that slug is taken"),
      new api.ApiError(503, "unavailable", "later"),
      new api.ApiError(0, "network_error", "offline"),
      new Error("boom"),
      "boom",
    ];
    for (const error of errors) {
      const { controller, notices, reloadCount } = harness();
      const attempt = deferred<never>();
      attempt.reject(error);
      const result = await controller.run(
        "update",
        () => attempt.promise,
        "Done.",
      );
      assert.deepEqual(result, { ok: false, error });
      assert.equal(reloadCount(), 0, String(error));
      assert.deepEqual(notices, [null], String(error));
    }
  });

  it("turns a call that throws before it returns a promise into a failure and frees the page", async () => {
    const { controller, busyKeys } = harness();
    const bug = new Error("not a promise");
    const result = await controller.run(
      "update",
      () => {
        throw bug;
      },
      "Done.",
    );
    assert.deepEqual(result, { ok: false, error: bug });
    assert.deepEqual(busyKeys, ["update", null]);
  });
});
