import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { FORM_ERROR_KEY } from "@/lib/app/forms";
import {
  EMPTY_DELEGATION_FORM,
  MEMORY_BUDGET_MAX_MB,
  MEMORY_BUDGET_MIN_MB,
  REQUEST_REASON_MAX_LENGTH,
  appChangeRequests,
  buildDelegationRequest,
  buildSettingsRequest,
  canWithdraw,
  delegationChoices,
  identityModes,
  limitRequests,
  pendingRequestOfKind,
  requestAnswerText,
  requestFormAvailability,
  requestKindLabel,
  requestSummaryLines,
  settingsFormDefaults,
} from "@/lib/app/requests";
import { readExample } from "../../../support/contract-examples";

const APP = api.AppDetail.parse(readExample("AppDetail"));
const EXAMPLE_REQUEST = api.RequestRow.parse(readExample("RequestRow"));

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

function request(overrides: Partial<api.RequestRow>): api.RequestRow {
  return { ...EXAMPLE_REQUEST, ...overrides };
}

function appWithDelegation(
  delegation: Partial<api.AppDetail["delegation"]>,
): api.AppDetail {
  return { ...APP, delegation: { ...APP.delegation, ...delegation } };
}

describe("settingsFormDefaults", () => {
  it("starts from the app's current values, as text, with no reason", () => {
    assert.deepEqual(settingsFormDefaults(APP), {
      slug: "sales",
      displayName: "Sales dashboard",
      sourcePath: "/Workspace/Shared/apps/sales",
      memoryBudgetMb: "512",
      reason: "",
    });
  });
});

describe("buildSettingsRequest", () => {
  const defaults = settingsFormDefaults(APP);

  function build(changes: Partial<typeof defaults>) {
    return buildSettingsRequest(APP, {
      ...defaults,
      reason: "the dashboard moved",
      ...changes,
    });
  }

  it("sends only the fields that changed", () => {
    const result = build({
      sourcePath: " /Workspace/Shared/apps/sales-v2 ",
      memoryBudgetMb: "1024",
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      kind: "settings",
      app: "sales",
      payload: {
        source_path: "/Workspace/Shared/apps/sales-v2",
        memory_budget_mb: 1024,
      },
      reason: "the dashboard moved",
    });
    assert.ok(api.CreatePermissionRequest.safeParse(result.body).success);
  });

  it("refuses a request with nothing changed and says so on the form", () => {
    const result = build({});
    assert.ok(!result.ok);
    assert.match(result.errors[FORM_ERROR_KEY] ?? "", /change at least one/i);
  });

  it("does not count surrounding spaces as a change", () => {
    const result = build({ displayName: "  Sales dashboard  " });
    assert.ok(!result.ok);
    assert.ok(result.errors[FORM_ERROR_KEY]);
  });

  it("asks for a reason", () => {
    const result = build({ displayName: "Revenue", reason: "   " });
    assert.ok(!result.ok);
    assert.match(result.errors.reason ?? "", /reason/i);
  });

  it("limits the reason to the contract's length", () => {
    const tooLong = build({
      displayName: "Revenue",
      reason: "x".repeat(REQUEST_REASON_MAX_LENGTH + 1),
    });
    assert.ok(!tooLong.ok);
    assert.equal(tooLong.errors.reason, "At most 500 characters");
    assert.ok(
      build({
        displayName: "Revenue",
        reason: "x".repeat(REQUEST_REASON_MAX_LENGTH),
      }).ok,
    );
  });

  it("checks a new slug like a registration does", () => {
    const result = build({ slug: "Not A Slug" });
    assert.ok(!result.ok);
    assert.ok(result.errors.slug);
    assert.equal(Object.keys(result.errors).length, 1);
  });

  it("checks the memory budget range at both edges", () => {
    assert.ok(build({ memoryBudgetMb: String(MEMORY_BUDGET_MIN_MB) }).ok);
    assert.ok(build({ memoryBudgetMb: String(MEMORY_BUDGET_MAX_MB) }).ok);
    for (const bad of [
      String(MEMORY_BUDGET_MIN_MB - 1),
      String(MEMORY_BUDGET_MAX_MB + 1),
    ]) {
      const result = build({ memoryBudgetMb: bad });
      assert.ok(!result.ok, bad);
      assert.ok(result.errors.memoryBudgetMb, bad);
    }
  });

  it("explains a budget that is not a whole number", () => {
    for (const bad of ["", "1e3", "12.5", "lots", "-5"]) {
      const result = build({ memoryBudgetMb: bad });
      assert.ok(!result.ok, bad);
      assert.match(result.errors.memoryBudgetMb ?? "", /whole number|Enter/i);
    }
  });

  it("refuses a source folder outside the workspace", () => {
    const result = build({ sourcePath: "/etc/passwd" });
    assert.ok(!result.ok);
    assert.ok(result.errors.sourcePath);
  });

  it("refuses a blank name", () => {
    const result = build({ displayName: "   " });
    assert.ok(!result.ok);
    assert.ok(result.errors.displayName);
  });
});

describe("delegationChoices", () => {
  it("offers OBO when it is not delegated, and says when it already is", () => {
    const choices = delegationChoices(
      appWithDelegation({ obo_delegated: true }),
    );
    assert.deepEqual(choices.obo, { state: "delegated" });
    const open = delegationChoices(appWithDelegation({ obo_delegated: false }));
    assert.deepEqual(open.obo, { state: "available" });
  });

  it("does not offer the shared service principal while the platform switch is off", () => {
    assert.deepEqual(
      delegationChoices(
        appWithDelegation({
          shared_sp_delegated: false,
          shared_sp_delegation_allowed: false,
        }),
      ).sharedSp,
      { state: "not_allowed" },
    );
    assert.deepEqual(
      delegationChoices(
        appWithDelegation({
          shared_sp_delegated: false,
          shared_sp_delegation_allowed: true,
        }),
      ).sharedSp,
      { state: "available" },
    );
  });

  it("reports a shared SP that is delegated as delegated, even if the switch is now off", () => {
    assert.deepEqual(
      delegationChoices(
        appWithDelegation({
          shared_sp_delegated: true,
          shared_sp_delegation_allowed: false,
        }),
      ).sharedSp,
      { state: "delegated" },
    );
  });
});

describe("identityModes", () => {
  function byId(app: api.AppDetail) {
    return Object.fromEntries(
      identityModes(app).map((mode) => [mode.id, mode]),
    );
  }

  it("lists OBO, the shared service principal and the app's own, in that order", () => {
    assert.deepEqual(
      identityModes(APP).map((mode) => mode.id),
      ["obo", "shared_sp", "own_sp"],
    );
  });

  it("says what is in effect for the example app", () => {
    const modes = byId(APP);
    assert.equal(modes.obo?.isInEffect, true);
    assert.equal(modes.obo?.stateText, "Delegated");
    assert.equal(modes.obo?.howToGet, null);
    assert.equal(modes.own_sp?.isInEffect, true);
    assert.equal(modes.own_sp?.stateText, "Configured");
  });

  it("says the shared service principal is switched off, and that a request for it is refused", () => {
    const sharedSp = byId(APP).shared_sp;
    assert.equal(sharedSp?.isInEffect, false);
    assert.equal(sharedSp?.isSwitchedOff, true);
    assert.match(sharedSp?.stateText ?? "", /switched off/i);
    assert.match(sharedSp?.howToGet ?? "", /platform admin/i);
  });

  it("points to a request when a delegation can be asked for", () => {
    const modes = byId(
      appWithDelegation({
        obo_delegated: false,
        shared_sp_delegation_allowed: true,
      }),
    );
    assert.equal(modes.obo?.stateText, "Not delegated");
    assert.match(modes.obo?.howToGet ?? "", /request a delegation/i);
    assert.equal(modes.shared_sp?.stateText, "Not delegated");
    assert.equal(modes.shared_sp?.isSwitchedOff, false);
    assert.match(modes.shared_sp?.howToGet ?? "", /request a delegation/i);
  });

  it("says how to give the app its own service principal when it has none", () => {
    const ownSp = byId({
      ...APP,
      config: {
        ...APP.config,
        own_sp_client_id_key: null,
        own_sp_client_secret_key: null,
      },
    }).own_sp;
    assert.equal(ownSp?.isInEffect, false);
    assert.equal(ownSp?.stateText, "Not configured");
    assert.match(ownSp?.howToGet ?? "", /secret/i);
  });
});

describe("buildDelegationRequest", () => {
  const open = appWithDelegation({
    obo_delegated: false,
    shared_sp_delegated: false,
    shared_sp_delegation_allowed: true,
  });

  it("asks for what is ticked, as true", () => {
    const result = buildDelegationRequest(open, {
      obo: true,
      sharedSp: true,
      reason: " needs the user's token ",
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body, {
      kind: "delegation",
      app: "sales",
      payload: { obo: true, shared_sp: true },
      reason: "needs the user's token",
    });
    assert.ok(api.CreatePermissionRequest.safeParse(result.body).success);
  });

  it("asks for one alone", () => {
    const result = buildDelegationRequest(open, {
      ...EMPTY_DELEGATION_FORM,
      obo: true,
      reason: "why",
    });
    assert.ok(result.ok);
    assert.deepEqual(
      result.body.kind === "delegation" ? result.body.payload : null,
      { obo: true },
    );
  });

  it("refuses an empty choice", () => {
    const result = buildDelegationRequest(open, {
      ...EMPTY_DELEGATION_FORM,
      reason: "why",
    });
    assert.ok(!result.ok);
    assert.match(result.errors[FORM_ERROR_KEY] ?? "", /choose/i);
  });

  it("refuses the shared SP while the platform switch is off, and an OBO that is already granted", () => {
    const closed = appWithDelegation({
      obo_delegated: true,
      shared_sp_delegated: false,
      shared_sp_delegation_allowed: false,
    });
    const result = buildDelegationRequest(closed, {
      obo: true,
      sharedSp: true,
      reason: "why",
    });
    assert.ok(!result.ok);
    assert.ok(result.errors.obo);
    assert.match(result.errors.sharedSp ?? "", /not allowed/i);
  });

  it("asks for a reason", () => {
    const result = buildDelegationRequest(open, {
      ...EMPTY_DELEGATION_FORM,
      obo: true,
      reason: "",
    });
    assert.ok(!result.ok);
    assert.match(result.errors.reason ?? "", /reason/i);
  });
});

describe("pendingRequestOfKind and requestFormAvailability", () => {
  const settingsPending = request({
    request_id: 10,
    kind: "settings",
    status: "pending",
  });
  const closedSettings = request({ request_id: 9, status: "denied" });
  const otherApp = request({
    request_id: 11,
    kind: "delegation",
    app_slug: "wiki",
    status: "pending",
  });
  const items = [closedSettings, settingsPending, otherApp];

  it("finds the one pending request of this app and kind", () => {
    assert.equal(
      pendingRequestOfKind(items, "sales", "settings")?.request_id,
      10,
    );
    assert.equal(pendingRequestOfKind(items, "sales", "delegation"), null);
    assert.equal(
      pendingRequestOfKind(items, "wiki", "delegation")?.request_id,
      11,
    );
  });

  it("opens the form when nothing of that kind is pending", () => {
    assert.deepEqual(
      requestFormAvailability("delegation", "sales", items, MANAGER),
      {
        isAllowed: true,
      },
    );
  });

  it("closes it while one is pending, and says to withdraw it first", () => {
    const availability = requestFormAvailability(
      "settings",
      "sales",
      items,
      MANAGER,
    );
    assert.ok(!availability.isAllowed);
    assert.match(availability.reason, /waiting/i);
    assert.match(availability.reason, /withdraw/i);
  });

  it("waits for the list and for the actor", () => {
    assert.ok(
      !requestFormAvailability("settings", "sales", null, MANAGER).isAllowed,
    );
    assert.ok(
      !requestFormAvailability("settings", "sales", items, null).isAllowed,
    );
  });

  it("is for an admin of the app", () => {
    assert.ok(
      !requestFormAvailability("settings", "sales", [], VISITOR).isAllowed,
    );
  });
});

describe("canWithdraw", () => {
  it("is for the asker, while the request is pending", () => {
    const pending = request({
      status: "pending",
      requested_by: "alice@example.com",
    });
    assert.equal(canWithdraw(pending, "alice@example.com"), true);
    assert.equal(canWithdraw(pending, "carol@example.com"), false);
    assert.equal(canWithdraw(pending, null), false);
    assert.equal(
      canWithdraw({ ...pending, status: "approved" }, "alice@example.com"),
      false,
    );
  });
});

describe("requestSummaryLines", () => {
  it("shows each requested value against the current one", () => {
    const row = request({
      kind: "settings",
      payload: { slug: "revenue", memory_budget_mb: 1024 },
      current: { slug: "sales", memory_budget_mb: 512 },
    });
    assert.deepEqual(requestSummaryLines(row), [
      "Slug: sales to revenue",
      "Memory budget: 512 MB to 1,024 MB",
    ]);
  });

  it("shows only the requested value when the current one is not given", () => {
    const row = request({
      kind: "settings",
      payload: { display_name: "Revenue" },
      current: null,
    });
    assert.deepEqual(requestSummaryLines(row), ["Display name: Revenue"]);
  });

  it("names the delegations asked for", () => {
    const row = request({
      kind: "delegation",
      payload: { obo: true, shared_sp: true },
      current: { obo: false, shared_sp: false },
    });
    assert.deepEqual(requestSummaryLines(row), [
      "Act on behalf of users (OBO)",
      "Act as the shared service principal",
    ]);
  });

  it("leaves out keys it does not know, and values that are not plain", () => {
    const row = request({
      kind: "settings",
      payload: { slug: { nested: true }, surprise: "x", display_name: "Ok" },
      current: null,
    });
    assert.deepEqual(requestSummaryLines(row), ["Display name: Ok"]);
  });
});

describe("limitRequests", () => {
  const rows = Array.from({ length: 12 }, (_, index) =>
    request({ request_id: 12 - index }),
  );

  it("keeps the newest few and counts the rest", () => {
    const limited = limitRequests(rows, 5);
    assert.equal(limited.shown.length, 5);
    assert.equal(limited.hiddenCount, 7);
    assert.equal(limited.shown[0]?.request_id, 12);
  });

  it("hides nothing in a short list", () => {
    assert.deepEqual(limitRequests(rows.slice(0, 3), 5).hiddenCount, 0);
  });
});

describe("requestAnswerText", () => {
  it("says a pending request waits for a platform admin", () => {
    assert.equal(
      requestAnswerText(request({ status: "pending" })),
      "Waiting for a platform admin",
    );
  });

  it("names who closed it and the note that came with the answer", () => {
    assert.equal(
      requestAnswerText(
        request({
          status: "denied",
          closed_by: "dana@example.com",
          closed_note: "use a new app instead",
        }),
      ),
      "Denied by dana@example.com: use a new app instead",
    );
    assert.equal(
      requestAnswerText(
        request({
          status: "approved",
          closed_by: "dana@example.com",
          closed_note: null,
        }),
      ),
      "Approved by dana@example.com",
    );
    assert.equal(
      requestAnswerText(
        request({
          status: "withdrawn",
          closed_by: "alice@example.com",
          closed_note: null,
        }),
      ),
      "Withdrawn by alice@example.com",
    );
  });

  it("does not invent a name when the server sent none", () => {
    assert.equal(
      requestAnswerText(
        request({ status: "denied", closed_by: null, closed_note: null }),
      ),
      "Denied",
    );
  });
});

describe("appChangeRequests", () => {
  it("keeps this app's settings and delegation requests, newest first", () => {
    const items = [
      request({ request_id: 1, app_slug: APP.slug, kind: "settings" }),
      request({ request_id: 2, app_slug: "other", kind: "settings" }),
      request({ request_id: 3, app_slug: APP.slug, kind: "delegation" }),
      request({ request_id: 4, app_slug: APP.slug, kind: "register" }),
    ];
    assert.deepEqual(
      appChangeRequests(items, APP.slug).map((row) => row.request_id),
      [3, 1],
    );
  });

  it("is empty when the app has none", () => {
    assert.deepEqual(appChangeRequests([], APP.slug), []);
  });
});

describe("requestKindLabel", () => {
  it("names the two kinds an app page lists", () => {
    assert.equal(requestKindLabel("settings"), "Settings change");
    assert.equal(requestKindLabel("delegation"), "Delegation");
    assert.equal(requestKindLabel("register"), "New app");
  });
});
