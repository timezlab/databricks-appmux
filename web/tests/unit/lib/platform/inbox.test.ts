import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { RequestRow } from "@/lib/api/schemas";
import {
  describeDecision,
  inboxItem,
  parseDecision,
  visibleText,
} from "@/lib/platform/inbox";
import type { InboxContext } from "@/lib/platform/inbox";

function row(overrides: Partial<RequestRow> = {}): RequestRow {
  return {
    request_id: 4,
    app_slug: "sales",
    kind: "settings",
    payload: { slug: "revenue" },
    reason: "the dashboard now covers all revenue",
    requested_by: "alice@example.com",
    requested_at: "2026-10-30T09:35:00Z",
    status: "pending",
    closed_by: null,
    closed_at: null,
    closed_note: null,
    current: { slug: "sales" },
    requires_reason: false,
    app_version: null,
    ...overrides,
  };
}

const CONTEXT: InboxContext = {
  apps: [{ slug: "sales" }, { slug: "reports" }],
  isSharedSpAllowed: false,
  freeMemoryMb: 1393,
};

function registerRow(payload: Record<string, unknown> = {}): RequestRow {
  return row({
    request_id: 6,
    app_slug: null,
    kind: "register",
    payload: {
      slug: "wiki",
      display_name: "Team wiki",
      source_path: "/Workspace/Shared/apps/wiki",
      memory_budget_mb: 128,
      ...payload,
    },
    current: null,
    requires_reason: true,
  });
}

describe("inboxItem: what is shown", () => {
  it("puts the current value next to the requested one for a settings change", () => {
    const item = inboxItem(row(), CONTEXT);
    assert.equal(item.kindLabel, "Settings change");
    assert.equal(item.appText, "sales");
    assert.equal(item.changes.length, 1);
    const [change] = item.changes;
    assert.equal(change?.label, "Slug (URL prefix)");
    assert.equal(change?.current, "sales");
    assert.equal(change?.requested, "revenue");
    assert.equal(change?.isGate, false);
    assert.match(change?.note ?? "", /old URL answers 404/i);
  });

  it("lists fields in a fixed order, whatever order the payload has, and marks the gate field", () => {
    const item = inboxItem(
      row({
        payload: {
          source_path: "/Workspace/Shared/apps/sales-v2",
          display_name: "Sales",
        },
        current: {
          display_name: "Sales dashboard",
          source_path: "/Workspace/Shared/apps/sales",
        },
        requires_reason: true,
      }),
      CONTEXT,
    );
    assert.deepEqual(
      item.changes.map((change) => [change.field, change.isGate]),
      [
        ["display_name", false],
        ["source_path", true],
      ],
    );
    assert.equal(item.requiresReason, true);
  });

  it("leaves out a field the request did not ask for", () => {
    const item = inboxItem(
      row({
        payload: { slug: null, display_name: "Revenue" },
        current: { display_name: "Sales" },
      }),
      CONTEXT,
    );
    assert.deepEqual(
      item.changes.map((change) => change.field),
      ["display_name"],
    );
  });

  it("writes a memory budget as megabytes", () => {
    const item = inboxItem(
      row({
        payload: { memory_budget_mb: 1024 },
        current: { memory_budget_mb: 512 },
      }),
      CONTEXT,
    );
    assert.equal(item.changes[0]?.current, "512 MB");
    assert.equal(item.changes[0]?.requested, "1,024 MB");
  });

  it("shows the proposed app of a registration, with all four fields and no current value", () => {
    const item = inboxItem(registerRow(), CONTEXT);
    assert.equal(item.kindLabel, "Register an app");
    assert.equal(item.appText, "wiki (new)");
    assert.deepEqual(
      item.changes.map((change) => change.field),
      ["slug", "display_name", "source_path", "memory_budget_mb"],
    );
    assert.ok(item.changes.every((change) => change.current === null));
    assert.ok(item.changes.every((change) => change.isGate));
    assert.match(item.changes[3]?.note ?? "", /1,393 MB free in the slot/);
  });

  it("says a missing field of a registration is missing, not blank", () => {
    const item = inboxItem(
      row({ ...registerRow(), payload: { slug: "wiki" } }),
      CONTEXT,
    );
    assert.equal(item.changes[1]?.requested, "(missing)");
  });

  it("writes a delegation as allowed and not allowed", () => {
    const item = inboxItem(
      row({
        app_slug: "docs",
        kind: "delegation",
        payload: { obo: true },
        current: { obo: false },
        requires_reason: true,
      }),
      CONTEXT,
    );
    assert.equal(item.kindLabel, "Delegation");
    assert.equal(item.changes[0]?.label, "OBO delegation");
    assert.equal(item.changes[0]?.current, "not allowed");
    assert.equal(item.changes[0]?.requested, "allowed");
    assert.equal(item.changes[0]?.isGate, true);
  });

  it("shows a field this page does not know instead of hiding it", () => {
    const item = inboxItem(
      row({ payload: { slug: "revenue", admin: true } }),
      CONTEXT,
    );
    const unknown = item.changes.find((change) => change.field === "admin");
    assert.equal(unknown?.isUnknown, true);
    assert.equal(unknown?.requested, "true");
    assert.equal(unknown?.current, "unknown");
    assert.equal(item.changes.at(-1)?.field, "admin");
  });
});

describe("inboxItem: text from other people", () => {
  it("makes control and direction characters visible, so what is approved is what is read", () => {
    assert.equal(visibleText("a‮b"), "a<U+202E>b");
    assert.equal(visibleText("a\u0000b"), "a<U+0000>b");
    assert.equal(visibleText("a\nb"), "a<U+000A>b");
    assert.equal(visibleText("a\nb", { keepLineBreaks: true }), "a\nb");
    assert.equal(
      visibleText("plain, ünïcode, 日本語"),
      "plain, ünïcode, 日本語",
    );
  });

  it("clips a long value and says how much is missing", () => {
    const item = inboxItem(
      row({
        payload: { display_name: "x".repeat(400) },
        current: { display_name: "y" },
      }),
      CONTEXT,
    );
    const requested = item.changes[0]?.requested ?? "";
    assert.ok(requested.startsWith("x".repeat(300)));
    assert.match(requested, /… \(100 more characters\)$/);
  });

  it("does not split a character when it clips", () => {
    const item = inboxItem(
      row({
        payload: { display_name: "😀".repeat(400) },
        current: { display_name: "y" },
      }),
      CONTEXT,
    );
    assert.doesNotMatch(
      item.changes[0]?.requested ?? "",
      /�|[\uD800-\uDBFF](?![\uDC00-\uDFFF])/,
    );
  });

  it("shows the asker's reason as written", () => {
    const item = inboxItem(row({ reason: "line one\nline two" }), CONTEXT);
    assert.equal(item.reason, "line one\nline two");
    assert.equal(item.requestedBy, "alice@example.com");
  });
});

describe("inboxItem: warnings", () => {
  it("warns that a taken slug will close the request as denied", () => {
    const item = inboxItem(registerRow({ slug: "sales" }), CONTEXT);
    assert.equal(item.warnings.length, 1);
    assert.match(item.warnings[0] ?? "", /'sales' already exists/);
    assert.match(item.warnings[0] ?? "", /denied/);
  });

  it("warns about a taken slug in a rename, but not when the slug is the app's own", () => {
    const rename = inboxItem(row({ payload: { slug: "reports" } }), CONTEXT);
    assert.equal(rename.warnings.length, 1);
    const own = inboxItem(row({ payload: { slug: "sales" } }), CONTEXT);
    assert.ok(
      own.warnings.every((warning) => !warning.includes("already exists")),
    );
  });

  it("warns that a shared-SP grant fails while the master switch is off", () => {
    const request = row({
      app_slug: "docs",
      kind: "delegation",
      payload: { shared_sp: true },
      current: { shared_sp: false },
      requires_reason: true,
    });
    const off = inboxItem(request, CONTEXT);
    assert.match(off.warnings[0] ?? "", /shared_sp_delegation_allowed/);
    const on = inboxItem(request, { ...CONTEXT, isSharedSpAllowed: true });
    assert.deepEqual(on.warnings, []);
  });

  it("warns when a new app's budget is larger than the free memory", () => {
    const item = inboxItem(registerRow({ memory_budget_mb: 2048 }), CONTEXT);
    assert.equal(item.warnings.length, 1);
    assert.match(item.warnings[0] ?? "", /2,048 MB/);
    assert.match(item.warnings[0] ?? "", /1,393 MB/);
  });

  it("compares only the increase when an existing app's budget grows", () => {
    const grow = (requested: number) =>
      inboxItem(
        row({
          payload: { memory_budget_mb: requested },
          current: { memory_budget_mb: 1024 },
        }),
        CONTEXT,
      );
    assert.deepEqual(grow(2000).warnings, []);
    assert.equal(grow(3000).warnings.length, 1);
  });

  it("says when approving would change nothing", () => {
    const item = inboxItem(
      row({
        payload: { display_name: "Sales" },
        current: { display_name: "Sales" },
      }),
      CONTEXT,
    );
    assert.match(item.warnings[0] ?? "", /already set/i);
  });
});

describe("parseDecision", () => {
  const gate = inboxItem(registerRow(), CONTEXT);
  const plain = inboxItem(row(), CONTEXT);

  it("requires a reason to approve a request that holds a gate field", () => {
    assert.deepEqual(
      parseDecision(gate, "approve", { reason: "  ", note: "" }),
      {
        ok: false,
        errors: { reason: "is required to approve this request" },
      },
    );
    assert.deepEqual(
      parseDecision(gate, "approve", { reason: " fine ", note: "" }),
      {
        ok: true,
        value: { reason: "fine" },
      },
    );
  });

  it("takes an optional reason and note otherwise, and leaves blank ones out", () => {
    assert.deepEqual(
      parseDecision(plain, "approve", { reason: "", note: "" }),
      {
        ok: true,
        value: {},
      },
    );
    assert.deepEqual(
      parseDecision(plain, "approve", { reason: "", note: " thanks " }),
      { ok: true, value: { note: "thanks" } },
    );
  });

  it("does not need a reason to deny, even a gate request", () => {
    assert.deepEqual(parseDecision(gate, "deny", { reason: "", note: "" }), {
      ok: true,
      value: {},
    });
    assert.deepEqual(
      parseDecision(gate, "deny", {
        reason: "",
        note: "the budget is too high",
      }),
      { ok: true, value: { note: "the budget is too high" } },
    );
  });
});

describe("describeDecision", () => {
  it("reports an approval", () => {
    const outcome = describeDecision(
      "approve",
      row({ status: "approved", app_version: 5 }),
    );
    assert.equal(outcome.tone, "success");
    assert.equal(outcome.title, "Approved request #4");
    assert.match(outcome.detail ?? "", /sales/);
  });

  it("reports a denial", () => {
    const outcome = describeDecision("deny", row({ status: "denied" }));
    assert.equal(outcome.tone, "info");
    assert.equal(outcome.title, "Denied request #4");
  });

  it("does not call an automatic denial an approval, and shows the server's note", () => {
    const outcome = describeDecision(
      "approve",
      row({
        status: "denied",
        closed_note: "slug 'reports' is already taken (automatic)",
      }),
    );
    assert.equal(outcome.tone, "warning");
    assert.match(outcome.title, /closed as denied/i);
    assert.equal(outcome.detail, "slug 'reports' is already taken (automatic)");
  });

  it("says what happened when the request ended up in another state", () => {
    const outcome = describeDecision("approve", row({ status: "withdrawn" }));
    assert.equal(outcome.tone, "warning");
    assert.match(outcome.title, /withdrawn/);
  });
});
