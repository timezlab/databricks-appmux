import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { appRequests, inboxRequests, ownRequests } from "@/lib/request-views";
import { readExample } from "../../support/contract-examples";

function exampleRow(): api.RequestRow {
  const [first] = api.RequestList.parse(readExample("RequestList")).items;
  assert.ok(first, "the RequestList example holds a row");
  return first;
}
const template = exampleRow();

function row(
  requestId: number,
  overrides: Partial<api.RequestRow> = {},
): api.RequestRow {
  return {
    ...template,
    request_id: requestId,
    status: "pending",
    requested_by: "alice@example.com",
    app_slug: "sales",
    ...overrides,
  };
}

/** What the server sends: oldest first. */
const SENT = [
  row(1, { requested_by: "bob@example.com", status: "approved" }),
  row(2, { app_slug: "wiki" }),
  row(3, { requested_by: "bob@example.com", app_slug: "wiki" }),
  row(4, { status: "denied" }),
  row(5, { app_slug: null, requested_by: "bob@example.com" }),
];

function ids(rows: api.RequestRow[]): number[] {
  return rows.map((request) => request.request_id);
}

describe("the contract's RequestList example", () => {
  it("is ordered oldest first, then by request_id", () => {
    const items = api.RequestList.parse(readExample("RequestList")).items;
    assert.ok(items.length > 1);
    for (const [index, next] of items.slice(1).entries()) {
      const previous = items[index];
      assert.ok(previous);
      assert.ok(
        previous.requested_at < next.requested_at ||
          (previous.requested_at === next.requested_at &&
            previous.request_id < next.request_id),
        `row ${next.request_id} follows row ${previous.request_id}`,
      );
    }
  });
});

describe("request views (design 9.4)", () => {
  it("the platform admin's inbox is the pending requests, oldest first", () => {
    assert.deepEqual(ids(inboxRequests(SENT)), [2, 3, 5]);
  });

  it("the launcher's own requests are the actor's, newest first", () => {
    assert.deepEqual(ids(ownRequests(SENT, "bob@example.com")), [5, 3, 1]);
    assert.deepEqual(ids(ownRequests(SENT, "alice@example.com")), [4, 2]);
    assert.deepEqual(ids(ownRequests(SENT, "nobody@example.com")), []);
  });

  it("an app's requests are those naming the app, newest first", () => {
    assert.deepEqual(ids(appRequests(SENT, "wiki")), [3, 2]);
    assert.deepEqual(ids(appRequests(SENT, "sales")), [4, 1]);
    assert.deepEqual(ids(appRequests(SENT, "missing")), []);
  });

  it("leaves the list it was given as it was", () => {
    const before = ids(SENT);
    inboxRequests(SENT);
    ownRequests(SENT, "bob@example.com");
    appRequests(SENT, "wiki");
    assert.deepEqual(ids(SENT), before);
  });
});
