import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import { CreatePermissionRequest, UpdateReleaseRequest } from "@/lib/api";
import {
  FORM_ERROR_KEY,
  SUGGESTION_DELAY_MS,
  createDebouncedSearch,
  errorsFromZod,
  friendlyIssueMessage,
  withoutError,
} from "@/lib/app/forms";
import type { SearchOutcome } from "@/lib/app/forms";

function failureOf(schema: {
  safeParse: (input: unknown) => {
    success: boolean;
    error?: Parameters<typeof errorsFromZod>[0];
  };
}) {
  return (input: unknown) => {
    const parsed = schema.safeParse(input);
    assert.ok(!parsed.success && parsed.error, "the input must be refused");
    return parsed.error;
  };
}

describe("friendlyIssueMessage", () => {
  const failTags = failureOf(UpdateReleaseRequest);

  it("reads a too-long string as a character limit", () => {
    const error = failTags({ name: "x".repeat(81), tags: [] });
    const [issue] = error.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "At most 80 characters");
  });

  it("reads a too-long array as an item limit", () => {
    const error = failTags({
      name: null,
      tags: Array.from({ length: 11 }, (_, index) => `t${index}`),
    });
    const [issue] = error.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "At most 10 items");
  });

  it("reads a number outside its range with the bound", () => {
    const failRequest = failureOf(CreatePermissionRequest);
    const small = failRequest({
      kind: "settings",
      app: "sales",
      payload: { memory_budget_mb: 8 },
      reason: "more room",
    });
    const [issue] = small.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "At least 64");
  });

  it("keeps the contract's own message for a custom rule", () => {
    const error = failTags({ name: null, tags: ["Stable"] });
    const [issue] = error.issues;
    assert.ok(issue);
    assert.equal(friendlyIssueMessage(issue), "must be lower case");
  });
});

describe("errorsFromZod", () => {
  const failRequest = failureOf(CreatePermissionRequest);

  it("keys each message by the field name, one message per field", () => {
    const error = failRequest({
      kind: "settings",
      app: "sales",
      payload: { slug: "Not A Slug", memory_budget_mb: 1 },
      reason: "  ",
    });
    assert.deepEqual(errorsFromZod(error, { dropPathPrefix: "payload" }), {
      slug: "lower-case letters, digits and -, at most 31 characters",
      memory_budget_mb: "At least 64",
      reason: "must not be blank",
    });
  });

  it("puts a rule that spans fields under the form key", () => {
    const error = failRequest({
      kind: "settings",
      app: "sales",
      payload: {},
      reason: "why",
    });
    assert.deepEqual(errorsFromZod(error, { dropPathPrefix: "payload" }), {
      [FORM_ERROR_KEY]: "ask for at least one field",
    });
  });

  it("can fold array positions into the field (tags.3 is tags)", () => {
    const error = UpdateReleaseRequest.safeParse({
      name: null,
      tags: ["ok", "Bad"],
    }).error;
    assert.ok(error);
    assert.deepEqual(errorsFromZod(error, { dropIndexes: true }), {
      tags: "must be lower case",
    });
    assert.deepEqual(Object.keys(errorsFromZod(error)), ["tags.1"]);
  });
});

describe("withoutError", () => {
  it("drops the field the person is editing and the form-wide message", () => {
    assert.deepEqual(
      withoutError(
        { slug: "bad", reason: "empty", [FORM_ERROR_KEY]: "all" },
        "slug",
      ),
      { reason: "empty" },
    );
  });

  it("returns the same object when there is nothing to drop", () => {
    const errors = { reason: "empty" };
    assert.equal(withoutError(errors, "slug"), errors);
  });
});

describe("createDebouncedSearch", () => {
  interface Call {
    text: string;
    signal: AbortSignal;
    resolve: (result: string) => void;
    reject: (error: unknown) => void;
  }

  let calls: Call[];
  let outcomes: SearchOutcome<string>[];

  function newSearch() {
    return createDebouncedSearch<string>({
      delayMs: SUGGESTION_DELAY_MS,
      run: (text, signal) =>
        new Promise<string>((resolve, reject) => {
          calls.push({ text, signal, resolve, reject });
        }),
      onOutcome: (outcome) => {
        outcomes.push(outcome);
      },
    });
  }

  beforeEach(() => {
    vi.useFakeTimers();
    calls = [];
    outcomes = [];
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("waits about a quarter of a second, and says it is loading meanwhile", () => {
    const search = newSearch();
    search.search("er");
    assert.deepEqual(outcomes, [{ status: "loading", text: "er" }]);
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS - 1);
    assert.equal(calls.length, 0);
    vi.advanceTimersByTime(1);
    assert.deepEqual(
      calls.map((call) => call.text),
      ["er"],
    );
    assert.equal(SUGGESTION_DELAY_MS, 250);
  });

  it("sends only the last of the keystrokes made within the delay", () => {
    const search = newSearch();
    search.search("e");
    vi.advanceTimersByTime(100);
    search.search("er");
    vi.advanceTimersByTime(100);
    search.search("eri");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    assert.deepEqual(
      calls.map((call) => call.text),
      ["eri"],
    );
  });

  it("reports the answer of the call it made", async () => {
    const search = newSearch();
    search.search("er");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    calls[0]?.resolve("two rows");
    await vi.runAllTimersAsync();
    assert.deepEqual(outcomes.at(-1), {
      status: "done",
      text: "er",
      result: "two rows",
    });
  });

  it("aborts the call in flight when the text changes, and never reports its answer", async () => {
    const search = newSearch();
    search.search("er");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    const first = calls[0];
    assert.ok(first);
    search.search("erin");
    assert.equal(first.signal.aborted, true);
    first.resolve("stale rows");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    calls[1]?.resolve("fresh rows");
    await vi.runAllTimersAsync();
    assert.deepEqual(
      outcomes.filter((outcome) => outcome.status === "done"),
      [{ status: "done", text: "erin", result: "fresh rows" }],
    );
  });

  it("reports a failure with its error, but not the abort it caused itself", async () => {
    const search = newSearch();
    search.search("er");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    const failure = new Error("offline");
    calls[0]?.reject(failure);
    await vi.runAllTimersAsync();
    assert.deepEqual(outcomes.at(-1), {
      status: "failed",
      text: "er",
      error: failure,
    });

    search.search("erin");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    search.search("erin n");
    calls[1]?.reject(new DOMException("aborted", "AbortError"));
    await vi.runAllTimersAsync();
    assert.equal(
      outcomes.filter((outcome) => outcome.status === "failed").length,
      1,
    );
  });

  it("stops everything on cancel: no call starts and nothing is reported", async () => {
    const search = newSearch();
    search.search("er");
    search.cancel();
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS * 2);
    assert.equal(calls.length, 0);

    search.search("erin");
    vi.advanceTimersByTime(SUGGESTION_DELAY_MS);
    const inFlight = calls[0];
    assert.ok(inFlight);
    search.cancel();
    assert.equal(inFlight.signal.aborted, true);
    inFlight.resolve("late rows");
    await vi.runAllTimersAsync();
    assert.equal(
      outcomes.some((outcome) => outcome.status === "done"),
      false,
    );
  });
});
