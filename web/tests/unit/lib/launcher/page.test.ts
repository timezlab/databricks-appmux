import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { ApiError, RequestList } from "@/lib/api";
import type { AppList, RequestRow } from "@/lib/api";
import {
  ME_POLL_INTERVAL_MS,
  accessBlockingError,
  appsEmptyCopy,
  registerFormContext,
} from "@/lib/launcher/page";
import { ownRequestsSection } from "@/lib/launcher/requests";
import {
  DEFAULT_POLL_INTERVAL_MS,
  MIN_POLL_INTERVAL_MS,
} from "@/lib/poll/schedule";
import type { PollResult } from "@/lib/poll/schedule";
import { readExample } from "../../../support/contract-examples";

const failed = (error: unknown): PollResult<AppList> => ({
  status: "failed",
  error,
});
const ready = <T>(data: T): PollResult<T> => ({
  status: "ready",
  data,
  updatedAtMs: 0,
});

describe("accessBlockingError: when the page has nothing to offer", () => {
  it("is the error when the first answer was 401 or 403", () => {
    const unauthorized = new ApiError(401, "x", "m");
    const forbidden = new ApiError(403, "x", "m");
    assert.equal(accessBlockingError(failed(unauthorized)), unauthorized);
    assert.equal(accessBlockingError(failed(forbidden)), forbidden);
  });

  it("is null for every other failure: those may pass, and the other sections may still load", () => {
    for (const status of [400, 404, 409, 500, 503, 0]) {
      assert.equal(
        accessBlockingError(failed(new ApiError(status, "x", "m"))),
        null,
        String(status),
      );
    }
    assert.equal(accessBlockingError(failed(new Error("boom"))), null);
  });

  it("is null while loading, once loaded, and when a later poll failed (the data stays)", () => {
    const data = { items: [] };
    assert.equal(accessBlockingError({ status: "loading" }), null);
    assert.equal(accessBlockingError(ready(data)), null);
    assert.equal(
      accessBlockingError({
        status: "stale",
        data,
        updatedAtMs: 0,
        error: new ApiError(401, "x", "m"),
      }),
      null,
    );
  });
});

describe("registerFormContext", () => {
  const items = RequestList.parse(readExample("RequestList")).items;
  const me = {
    email: "frank@example.com",
    groups: [],
    is_platform_admin: false,
  };
  const apps = ready({
    items: [
      {
        slug: "sales",
        display_name: "Sales",
        state: "running" as const,
        permission: "CAN_USE" as const,
        open_modes: [],
      },
    ],
  });

  it("knows the slugs of the visible apps and the visitor's pending register slugs", () => {
    const section = ownRequestsSection(ready(me), ready({ items }));
    // frank@example.com has a pending delegation (not a register) and a pending register for "wiki".
    assert.deepEqual(registerFormContext(apps, section), {
      knownSlugs: ["sales"],
      pendingRegisterSlugs: ["wiki"],
    });
  });

  it("leaves the pending slugs unknown while the requests are not loaded, so the server decides", () => {
    const section = ownRequestsSection(
      { status: "loading" },
      { status: "loading" },
    );
    assert.deepEqual(registerFormContext(apps, section), {
      knownSlugs: ["sales"],
      pendingRegisterSlugs: null,
    });
  });

  it("knows no slugs while the apps are not loaded", () => {
    const section = ownRequestsSection(
      ready(me),
      ready({ items: [] as RequestRow[] }),
    );
    assert.deepEqual(registerFormContext({ status: "loading" }, section), {
      knownSlugs: [],
      pendingRegisterSlugs: [],
    });
  });

  it("only counts the visitor's own requests", () => {
    const section = ownRequestsSection(
      ready({ ...me, email: "someone-else@example.com" }),
      ready({ items }),
    );
    assert.deepEqual(
      registerFormContext(apps, section).pendingRegisterSlugs,
      [],
    );
  });
});

describe("appsEmptyCopy", () => {
  it("tells a visitor how an app comes to be on their list", () => {
    const copy = appsEmptyCopy(false);
    assert.match(copy.title, /no apps/i);
    assert.match(copy.body, /share/i);
    assert.match(copy.body, /request/i);
  });

  it("tells a platform admin that nothing is registered, and where to register one", () => {
    const copy = appsEmptyCopy(true);
    assert.match(copy.title, /no apps/i);
    assert.match(copy.body, /platform/i);
  });

  it("does not guess the role while it is unknown", () => {
    assert.deepEqual(appsEmptyCopy(null), appsEmptyCopy(false));
  });
});

describe("the identity poll", () => {
  it("is slower than the lists and not below the poll minimum", () => {
    assert.ok(ME_POLL_INTERVAL_MS > DEFAULT_POLL_INTERVAL_MS);
    assert.ok(ME_POLL_INTERVAL_MS >= MIN_POLL_INTERVAL_MS);
  });
});
