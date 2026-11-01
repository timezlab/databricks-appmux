import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { RequestList, RequestRow } from "@/lib/api";
import type { MeResponse } from "@/lib/api";
import {
  MAX_PENDING_REGISTER_REQUESTS,
  PLATFORM_INBOX_HREF,
  RECENT_CLOSED_REQUESTS_SHOWN,
  adminPendingCount,
  answeredRequestsMessage,
  canWithdraw,
  describeRequest,
  olderAnswersToggleLabel,
  ownRequestsSection,
  pendingBannerText,
  pendingCountText,
  pendingRegisterSlugs,
  reviewerNote,
  shouldShowOwnRequests,
  visibleOwnRequests,
  withdrawnMessage,
} from "@/lib/launcher/requests";
import type { PollResult } from "@/lib/poll/schedule";
import { readExample } from "../../../support/contract-examples";

const ME = "frank@example.com";

const template = RequestList.parse(readExample("RequestList")).items[0];
assert.ok(template, "the RequestList example holds a row");

function row(
  requestId: number,
  overrides: Partial<RequestRow> = {},
): RequestRow {
  // Parsed, so a fixture that breaks the contract fails here, not in a test.
  return RequestRow.parse({
    ...template,
    request_id: requestId,
    app_slug: "sales",
    kind: "settings",
    payload: { slug: "revenue" },
    current: { slug: "sales" },
    requested_by: ME,
    requested_at: "2026-10-30T09:00:00Z",
    status: "pending",
    closed_by: null,
    closed_at: null,
    closed_note: null,
    requires_reason: false,
    app_version: null,
    ...overrides,
  });
}

function registerRow(
  requestId: number,
  slug: string,
  overrides: Partial<RequestRow> = {},
): RequestRow {
  return row(requestId, {
    kind: "register",
    app_slug: null,
    payload: {
      slug,
      display_name: "Team wiki",
      source_path: "/Workspace/Shared/apps/wiki",
      memory_budget_mb: 128,
    },
    current: null,
    requires_reason: true,
    ...overrides,
  });
}

function ids(rows: readonly RequestRow[]): number[] {
  return rows.map((request) => request.request_id);
}

const ready = <T>(data: T): PollResult<T> => ({
  status: "ready",
  data,
  updatedAtMs: 0,
});
const stale = <T>(data: T, error: unknown): PollResult<T> => ({
  status: "stale",
  data,
  error,
  updatedAtMs: 0,
});
const me = (overrides: Partial<MeResponse> = {}): MeResponse => ({
  email: ME,
  groups: [],
  is_platform_admin: false,
  ...overrides,
});

describe("describeRequest", () => {
  it("a register request names the app, its slug, folder and budget", () => {
    const described = describeRequest(registerRow(1, "wiki"));
    assert.equal(described.summary, "New app: Team wiki");
    assert.equal(described.shortLabel, "new app wiki");
    assert.deepEqual(described.details, [
      "Slug: wiki",
      "Source folder: /Workspace/Shared/apps/wiki",
      "Memory budget: 128 MB",
    ]);
  });

  it("a delegation request says which credential, per flag asked", () => {
    const described = describeRequest(
      row(2, {
        kind: "delegation",
        payload: { obo: true, shared_sp: true },
        current: { obo: false, shared_sp: false },
      }),
    );
    assert.equal(described.summary, "Delegation for sales");
    assert.equal(described.shortLabel, "delegation for sales");
    assert.equal(described.details.length, 2);
    assert.match(described.details[0] ?? "", /OBO/);
    assert.match(described.details[1] ?? "", /service principal/i);
  });

  it("a settings request shows the current value next to the requested one", () => {
    const described = describeRequest(
      row(3, {
        payload: { memory_budget_mb: 256, slug: "revenue" },
        current: { memory_budget_mb: 128, slug: "sales" },
      }),
    );
    assert.equal(described.summary, "Settings change for sales");
    assert.deepEqual(described.details, [
      "Slug: sales → revenue",
      "Memory budget: 128 MB → 256 MB",
    ]);
  });

  it("a settings request with no current value shows only the requested one", () => {
    const described = describeRequest(
      row(3, { payload: { display_name: "Revenue" }, current: null }),
    );
    assert.deepEqual(described.details, ["Name: Revenue"]);
  });

  it("shows a key it does not know under its own name, as text", () => {
    const described = describeRequest(
      row(3, { payload: { colour: "<b>red</b>" }, current: null }),
    );
    assert.deepEqual(described.details, ["colour: <b>red</b>"]);
  });

  it("shows a value it cannot read as unknown, never as an object dump", () => {
    const described = describeRequest(
      row(3, { payload: { slug: { nested: 1 } }, current: null }),
    );
    assert.deepEqual(described.details, ["Slug: unknown"]);
  });

  it("falls back to the row's app when the payload has no slug", () => {
    const described = describeRequest(
      registerRow(1, "wiki", { payload: {}, app_slug: "wiki" }),
    );
    assert.equal(described.shortLabel, "new app wiki");
  });
});

describe("withdraw", () => {
  it("only the asker may withdraw, and only while pending", () => {
    assert.equal(canWithdraw(row(1), ME), true);
    assert.equal(canWithdraw(row(1), "bob@example.com"), false);
    for (const status of ["approved", "denied", "withdrawn"] as const) {
      assert.equal(canWithdraw(row(1, { status }), ME), false, status);
    }
  });

  it("the confirmation names the request", () => {
    assert.match(withdrawnMessage(registerRow(1, "wiki")), /new app wiki/);
  });
});

describe("the reviewer's note", () => {
  it("is shown for an answered request that has one", () => {
    const denied = row(1, {
      status: "denied",
      closed_note: "slug 'reports' is already taken (automatic)",
    });
    assert.equal(
      reviewerNote(denied),
      "slug 'reports' is already taken (automatic)",
    );
    assert.equal(
      reviewerNote({ ...denied, status: "approved" }),
      denied.closed_note,
    );
  });

  it("is absent while pending, after a withdrawal, or when blank", () => {
    assert.equal(reviewerNote(row(1, { closed_note: "x" })), null);
    assert.equal(
      reviewerNote(row(1, { status: "withdrawn", closed_note: "x" })),
      null,
    );
    assert.equal(
      reviewerNote(row(1, { status: "denied", closed_note: "  " })),
      null,
    );
    assert.equal(reviewerNote(row(1, { status: "denied" })), null);
  });
});

describe("visibleOwnRequests (newest first, history bounded)", () => {
  const sent = [
    row(1, { requested_by: "bob@example.com" }),
    row(2),
    row(3, { status: "approved" }),
    row(4, { requested_by: "bob@example.com" }),
    row(5, { status: "denied" }),
  ];

  it("keeps the asker's own, newest first", () => {
    const view = visibleOwnRequests(sent, ME, false);
    assert.deepEqual(ids(view.rows), [5, 3, 2]);
    assert.equal(view.hiddenCount, 0);
  });

  it("shows every pending request but only the newest closed ones, and counts the rest", () => {
    const history = [
      row(1),
      ...Array.from({ length: RECENT_CLOSED_REQUESTS_SHOWN + 3 }, (_, index) =>
        row(index + 2, { status: "approved" }),
      ),
    ];
    const view = visibleOwnRequests(history, ME, false);
    assert.equal(view.hiddenCount, 3);
    assert.equal(view.rows.length, RECENT_CLOSED_REQUESTS_SHOWN + 1);
    assert.ok(ids(view.rows).includes(1), "the old pending request stays");
    assert.deepEqual(ids(view.rows).slice(0, 2), [
      RECENT_CLOSED_REQUESTS_SHOWN + 4,
      RECENT_CLOSED_REQUESTS_SHOWN + 3,
    ]);
  });

  it("shows the whole history when asked", () => {
    const history = Array.from({ length: 25 }, (_, index) =>
      row(index + 1, { status: "approved" }),
    );
    const view = visibleOwnRequests(history, ME, true);
    assert.equal(view.rows.length, 25);
    assert.equal(view.hiddenCount, 0);
  });

  it("is empty for a person who asked for nothing", () => {
    assert.deepEqual(visibleOwnRequests(sent, "nobody@example.com", false), {
      rows: [],
      hiddenCount: 0,
    });
  });
});

describe("olderAnswersToggleLabel (the button for answered requests beyond the latest)", () => {
  it("offers nothing while no answered request is hidden", () => {
    assert.equal(olderAnswersToggleLabel(0, false), null);
  });

  it("offers to show the hidden ones, with their number", () => {
    assert.equal(olderAnswersToggleLabel(3, false), "Show 3 older answered");
  });

  it("offers to go back once everything is shown, even though nothing is hidden then", () => {
    assert.equal(
      olderAnswersToggleLabel(0, true),
      "Show only the latest answers",
    );
  });
});

describe("the register cap and the slugs already asked for", () => {
  it("is 3 pending register requests per person (FR-041)", () => {
    assert.equal(MAX_PENDING_REGISTER_REQUESTS, 3);
  });

  it("lists the proposed slugs of the pending register requests, and no others", () => {
    const rows = [
      registerRow(1, "wiki"),
      registerRow(2, "old", { status: "denied" }),
      row(3),
      registerRow(4, "blog"),
    ];
    assert.deepEqual(pendingRegisterSlugs(rows), ["wiki", "blog"]);
  });

  it("skips a register request whose payload has no readable slug", () => {
    assert.deepEqual(
      pendingRegisterSlugs([registerRow(1, "x", { payload: { slug: 7 } })]),
      [],
    );
  });
});

describe("the platform admin's pending count", () => {
  it("counts the pending requests of everyone, from the same list", () => {
    const requests = ready(
      RequestList.parse({
        items: [
          row(1),
          row(2, { requested_by: "bob@example.com" }),
          row(3, { status: "approved" }),
        ],
      }),
    );
    assert.equal(
      adminPendingCount(ready(me({ is_platform_admin: true })), requests),
      2,
    );
  });

  it("is absent for a visitor, even one who manages an app and so sees other people's requests", () => {
    const requests = ready({
      items: [row(1, { requested_by: "bob@example.com" })],
    });
    assert.equal(adminPendingCount(ready(me()), requests), null);
  });

  it("is absent until both answers are in, and keeps the last value when a poll fails", () => {
    const admin = ready(me({ is_platform_admin: true }));
    const requests = ready({ items: [row(1)] });
    assert.equal(adminPendingCount({ status: "loading" }, requests), null);
    assert.equal(adminPendingCount(admin, { status: "loading" }), null);
    assert.equal(
      adminPendingCount(admin, { status: "failed", error: new Error("x") }),
      null,
    );
    assert.equal(
      adminPendingCount(admin, stale({ items: [row(1)] }, new Error("x"))),
      1,
    );
  });

  it("reads in words, with the singular", () => {
    assert.equal(
      pendingCountText(0),
      "No requests are waiting for a decision.",
    );
    assert.equal(pendingCountText(1), "1 request is waiting for a decision.");
    assert.equal(pendingCountText(4), "4 requests are waiting for a decision.");
  });

  it("has a banner only when something is waiting: a platform admin with an empty inbox sees nothing", () => {
    assert.equal(pendingBannerText(null), null);
    assert.equal(pendingBannerText(0), null);
    assert.equal(pendingBannerText(1), "1 request is waiting for a decision.");
    assert.equal(
      pendingBannerText(4),
      "4 requests are waiting for a decision.",
    );
  });

  it("links to the inbox of the platform page", () => {
    assert.equal(PLATFORM_INBOX_HREF, "/platform/#inbox");
  });
});

describe("answeredRequestsMessage (what a poll changes)", () => {
  const before = [
    registerRow(1, "wiki"),
    row(2),
    row(3, { requested_by: "bob@example.com" }),
  ];

  it("says nothing on the first load", () => {
    assert.equal(answeredRequestsMessage(null, before, ME), null);
  });

  it("says nothing when nothing changed", () => {
    assert.equal(answeredRequestsMessage(before, [...before], ME), null);
  });

  it("announces an approval of the person's own request", () => {
    const after = [
      registerRow(1, "wiki", { status: "approved" }),
      ...before.slice(1),
    ];
    assert.equal(
      answeredRequestsMessage(before, after, ME),
      "Your request for new app wiki was approved.",
    );
  });

  it("announces a denial with the reviewer's note", () => {
    const after = [
      registerRow(1, "wiki"),
      row(2, { status: "denied", closed_note: "use the docs app" }),
      before[2] ?? row(3),
    ];
    assert.equal(
      answeredRequestsMessage(before, after, ME),
      "Your request for settings change for sales was denied: use the docs app.",
    );
  });

  it("ignores a withdrawal, and answers to other people's requests", () => {
    const after = [
      registerRow(1, "wiki", { status: "withdrawn" }),
      row(2),
      row(3, { requested_by: "bob@example.com", status: "approved" }),
    ];
    assert.equal(answeredRequestsMessage(before, after, ME), null);
  });

  it("ignores a request that was already answered before", () => {
    const answered = [registerRow(1, "wiki", { status: "approved" })];
    assert.equal(answeredRequestsMessage(answered, answered, ME), null);
  });

  it("lists a few and counts the rest", () => {
    const pending = Array.from({ length: 5 }, (_, index) =>
      registerRow(index + 1, `app-${index + 1}`),
    );
    const answered = pending.map((request) => ({
      ...request,
      status: "approved" as const,
    }));
    const message = answeredRequestsMessage(pending, answered, ME) ?? "";
    assert.equal(message.match(/was approved/g)?.length, 3);
    assert.match(message, /2 more/);
  });
});

describe("ownRequestsSection (which answer the section waits for)", () => {
  const list = { items: [row(1)] };

  it("is loading until both identity and requests are in", () => {
    assert.deepEqual(ownRequestsSection({ status: "loading" }, ready(list)), {
      status: "loading",
    });
    assert.deepEqual(ownRequestsSection(ready(me()), { status: "loading" }), {
      status: "loading",
    });
  });

  it("is ready with the actor's email and the whole list", () => {
    assert.deepEqual(ownRequestsSection(ready(me()), ready(list)), {
      status: "ready",
      email: ME,
      items: list.items,
      stale: null,
    });
  });

  it("fails when either answer failed, and says which error", () => {
    const meError = new Error("me");
    const listError = new Error("list");
    assert.deepEqual(
      ownRequestsSection({ status: "failed", error: meError }, ready(list)),
      { status: "failed", error: meError },
    );
    assert.deepEqual(
      ownRequestsSection(ready(me()), { status: "failed", error: listError }),
      { status: "failed", error: listError },
    );
    assert.deepEqual(
      ownRequestsSection(
        { status: "failed", error: meError },
        { status: "failed", error: listError },
      ),
      { status: "failed", error: meError },
    );
  });

  it("keeps showing the last list when a later poll fails, and carries the error", () => {
    const error = new Error("late");
    const section = ownRequestsSection(ready(me()), stale(list, error));
    assert.deepEqual(section, {
      status: "ready",
      email: ME,
      items: list.items,
      stale: { error },
    });
  });

  it("a failed identity poll after a good one keeps the section too", () => {
    const error = new Error("late");
    const section = ownRequestsSection(stale(me(), error), ready(list));
    assert.equal(section.status, "ready");
    assert.deepEqual(section.status === "ready" ? section.stale : null, {
      error,
    });
  });
});

describe("shouldShowOwnRequests (the section appears only when there is something to show)", () => {
  const section = (items: RequestRow[]) =>
    ownRequestsSection(ready(me()), ready({ items }));

  it("is hidden while the answers are loading, so a visitor with no requests never sees it flash", () => {
    assert.equal(shouldShowOwnRequests({ status: "loading" }), false);
  });

  it("is hidden for a person who has asked for nothing", () => {
    assert.equal(shouldShowOwnRequests(section([])), false);
  });

  it("is hidden when the list holds only other people's requests", () => {
    const others = [row(1, { requested_by: "bob@example.com" })];
    assert.equal(shouldShowOwnRequests(section(others)), false);
  });

  it("is shown once the person has made a request, answered or not", () => {
    assert.equal(shouldShowOwnRequests(section([row(1)])), true);
    assert.equal(
      shouldShowOwnRequests(section([row(1, { status: "withdrawn" })])),
      true,
    );
  });

  it("is shown when loading failed, so the failure is not silent", () => {
    assert.equal(
      shouldShowOwnRequests({ status: "failed", error: new Error("x") }),
      true,
    );
  });
});
