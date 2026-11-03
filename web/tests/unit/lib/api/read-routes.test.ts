/**
 * The typed functions of the read routes added for the admin UI: the URL and
 * query each sends, the contract example as the response, and the two ways a
 * call fails closed (a query the schema refuses never reaches `fetch`; a body in
 * the wrong shape is `invalid_response`).
 */
import assert from "node:assert/strict";
import path from "node:path";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import * as api from "@/lib/api";
import { EXAMPLES_DIR, readJson } from "../../../support/contract-examples";
import {
  jsonResponse,
  type RecordedCall,
  stubFetch,
} from "../../../support/fake-fetch";

const PREFIX = "/_appmux/api/v1";

describe("read routes of the admin UI", () => {
  let calls: RecordedCall[];
  let respond: () => Response | Promise<Response>;

  beforeEach(() => {
    calls = [];
    respond = () =>
      jsonResponse(500, { error: { code: "test", message: "stub" } });
    stubFetch((call) => {
      calls.push(call);
      return respond();
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function lastCall(): RecordedCall {
    const call = calls.at(-1);
    assert.ok(call, "fetch was not called");
    return call;
  }

  function example(name: string): unknown {
    return readJson(path.join(EXAMPLES_DIR, `${name}.json`));
  }

  /** The call is refused as a bad query, before `fetch`. */
  async function assertRefusedBeforeSending(
    call: () => Promise<unknown>,
    label: string,
  ): Promise<void> {
    const before = calls.length;
    await assert.rejects(call(), (error: unknown) => {
      assert.ok(error instanceof api.ApiError, label);
      assert.equal(error.status, 0, label);
      assert.equal(error.code, api.INVALID_REQUEST, label);
      return true;
    });
    assert.equal(calls.length, before, `${label}: nothing was sent`);
  }

  /** The server answered 200 with a body the response schema refuses. */
  async function assertInvalidResponse(
    call: () => Promise<unknown>,
    body: unknown,
  ): Promise<void> {
    respond = () => jsonResponse(200, body);
    await assert.rejects(call(), (error: unknown) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, 200);
      assert.equal(error.code, api.INVALID_RESPONSE);
      return true;
    });
  }

  describe("searchPrincipals", () => {
    it("sends GET /principals with no query string for no query", async () => {
      respond = () => jsonResponse(200, example("PrincipalList"));
      const list = await api.searchPrincipals();
      assert.equal(lastCall().url, `${PREFIX}/principals`);
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(list, api.PrincipalList.parse(example("PrincipalList")));
    });

    it("sends types as one comma list, in the order given, and encodes q strictly", async () => {
      respond = () => jsonResponse(200, example("PrincipalList"));
      await api.searchPrincipals({
        q: "ali ce+x@example.com",
        types: ["user", "service_principal"],
        limit: 5,
      });
      assert.equal(
        lastCall().url,
        `${PREFIX}/principals?q=ali%20ce%2Bx%40example.com&types=user%2Cservice_principal&limit=5`,
      );
    });

    it("sends an empty q, which reads no directory", async () => {
      respond = () => jsonResponse(200, example("PrincipalList"));
      await api.searchPrincipals({ q: "" });
      assert.equal(lastCall().url, `${PREFIX}/principals?q=`);
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("PrincipalList"));
      const controller = new AbortController();
      await api.searchPrincipals({}, { signal: controller.signal });
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("refuses a query the schema refuses, before sending", async () => {
      await assertRefusedBeforeSending(
        () => api.searchPrincipals({ q: "a".repeat(65) }),
        "q too long",
      );
      await assertRefusedBeforeSending(
        () => api.searchPrincipals({ q: "a'b" }),
        "q with a quote",
      );
      await assertRefusedBeforeSending(
        () => api.searchPrincipals({ types: [] }),
        "no types",
      );
      await assertRefusedBeforeSending(
        () => api.searchPrincipals({ limit: 51 }),
        "limit over 50",
      );
    });

    it("fails closed on a body in the wrong shape", async () => {
      await assertInvalidResponse(() => api.searchPrincipals(), {
        items: [],
      });
    });
  });

  describe("browseWorkspace", () => {
    it("sends GET /workspace/browse with no query string for the roots", async () => {
      respond = () => jsonResponse(200, example("WorkspaceListing"));
      const listing = await api.browseWorkspace();
      assert.equal(lastCall().url, `${PREFIX}/workspace/browse`);
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(
        listing,
        api.WorkspaceListing.parse(example("WorkspaceListing")),
      );
    });

    it("sends the path percent-encoded, slashes and spaces included", async () => {
      respond = () => jsonResponse(200, example("WorkspaceListing"));
      await api.browseWorkspace({ path: "/Workspace/Shared/my app" });
      assert.equal(
        lastCall().url,
        `${PREFIX}/workspace/browse?path=%2FWorkspace%2FShared%2Fmy%20app`,
      );
    });

    it("sends a path outside /Workspace/, which only the server judges", async () => {
      respond = () => jsonResponse(200, example("WorkspaceListing"));
      await api.browseWorkspace({ path: "/tmp" });
      assert.equal(lastCall().url, `${PREFIX}/workspace/browse?path=%2Ftmp`);
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("WorkspaceListing"));
      const controller = new AbortController();
      await api.browseWorkspace({}, { signal: controller.signal });
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("refuses a path that can never be sent, before sending", async () => {
      await assertRefusedBeforeSending(
        () => api.browseWorkspace({ path: "/Workspace/" + "a".repeat(512) }),
        "path over 512 characters",
      );
      await assertRefusedBeforeSending(
        () => api.browseWorkspace({ path: "/Workspace/a\nb" }),
        "path with a control character",
      );
    });

    it("fails closed on a body in the wrong shape", async () => {
      await assertInvalidResponse(() => api.browseWorkspace(), {
        path: null,
        entries: [],
      });
    });
  });

  describe("getAppLogs", () => {
    it("sends GET /apps/{app}/logs with no query string for no query", async () => {
      respond = () => jsonResponse(200, example("AppLogs"));
      const logs = await api.getAppLogs("sales");
      assert.equal(lastCall().url, `${PREFIX}/apps/sales/logs`);
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(logs, api.AppLogs.parse(example("AppLogs")));
    });

    it("sends after and limit, and after 0 as a value and not as an absence", async () => {
      respond = () => jsonResponse(200, example("AppLogs"));
      await api.getAppLogs("sales", { after: 12, limit: 200 });
      assert.equal(
        lastCall().url,
        `${PREFIX}/apps/sales/logs?after=12&limit=200`,
      );

      await api.getAppLogs("sales", { after: 0 });
      assert.equal(lastCall().url, `${PREFIX}/apps/sales/logs?after=0`);
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("AppLogs"));
      const controller = new AbortController();
      await api.getAppLogs("sales", {}, { signal: controller.signal });
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("refuses a query the schema refuses, before sending", async () => {
      await assertRefusedBeforeSending(
        () => api.getAppLogs("sales", { after: -1 }),
        "after below 0",
      );
      await assertRefusedBeforeSending(
        () => api.getAppLogs("sales", { after: 1.5 }),
        "after not whole",
      );
      await assertRefusedBeforeSending(
        () => api.getAppLogs("sales", { limit: 1001 }),
        "limit over 1000",
      );
      await assertRefusedBeforeSending(
        () => api.getAppLogs("sales", { limit: 0 }),
        "limit 0",
      );
    });

    it("refuses an app that is no path segment, before sending", async () => {
      await assert.rejects(api.getAppLogs("a/b"), /path segment/);
      assert.equal(calls.length, 0);
    });

    it("fails closed on a body in the wrong shape", async () => {
      await assertInvalidResponse(() => api.getAppLogs("sales"), {
        ...(example("AppLogs") as object),
        lines: [{ seq: 1, text: "no stream or time" }],
      });
    });
  });

  describe("getAppEnvironment", () => {
    it("sends GET /apps/{app}/environment and parses the example", async () => {
      respond = () => jsonResponse(200, example("AppEnvironment"));
      const environment = await api.getAppEnvironment("sales");
      assert.equal(lastCall().url, `${PREFIX}/apps/sales/environment`);
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(
        environment,
        api.AppEnvironment.parse(example("AppEnvironment")),
      );
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("AppEnvironment"));
      const controller = new AbortController();
      await api.getAppEnvironment("sales", { signal: controller.signal });
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("fails closed on a body in the wrong shape, and on a secret that carries its value", async () => {
      await assertInvalidResponse(() => api.getAppEnvironment("sales"), {
        release: 1,
      });

      const body = example("AppEnvironment") as {
        variables: Record<string, unknown>[];
      };
      const leaking = {
        ...body,
        variables: [
          {
            name: "TOKEN",
            value: "hunter2",
            source: "resource",
            resource_key: "token",
            is_secret: true,
          },
        ],
      };
      await assertInvalidResponse(
        () => api.getAppEnvironment("sales"),
        leaking,
      );
    });
  });

  describe("listResourceTypes", () => {
    it("sends GET /resources/types and parses the example", async () => {
      respond = () => jsonResponse(200, example("ResourceTypeList"));
      const types = await api.listResourceTypes();
      assert.equal(lastCall().url, `${PREFIX}/resources/types`);
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(
        types,
        api.ResourceTypeList.parse(example("ResourceTypeList")),
      );
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("ResourceTypeList"));
      const controller = new AbortController();
      await api.listResourceTypes({ signal: controller.signal });
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("fails closed on a body in the wrong shape", async () => {
      await assertInvalidResponse(() => api.listResourceTypes(), {
        items: [{ type: "secret" }],
      });
    });
  });

  describe("suggestResources", () => {
    it("sends GET /resources/suggestions with type, q and limit in that order", async () => {
      respond = () => jsonResponse(200, example("ResourceSuggestionList"));
      const suggestions = await api.suggestResources({
        type: "secret",
        q: "my-scope/",
        limit: 20,
      });
      assert.equal(
        lastCall().url,
        `${PREFIX}/resources/suggestions?type=secret&q=my-scope%2F&limit=20`,
      );
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(
        suggestions,
        api.ResourceSuggestionList.parse(example("ResourceSuggestionList")),
      );
    });

    it("sends only the type when that is all there is", async () => {
      respond = () => jsonResponse(200, example("ResourceSuggestionList"));
      await api.suggestResources({ type: "sql_warehouse" });
      assert.equal(
        lastCall().url,
        `${PREFIX}/resources/suggestions?type=sql_warehouse`,
      );
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("ResourceSuggestionList"));
      const controller = new AbortController();
      await api.suggestResources(
        { type: "job" },
        { signal: controller.signal },
      );
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("refuses a query the schema refuses, before sending", async () => {
      await assertRefusedBeforeSending(
        () => api.suggestResources({ type: "" }),
        "blank type",
      );
      await assertRefusedBeforeSending(
        // @ts-expect-error: a page cannot omit the type; the test proves the schema backs the compiler.
        () => api.suggestResources({}),
        "no type",
      );
      await assertRefusedBeforeSending(
        () => api.suggestResources({ type: "job", q: "a".repeat(257) }),
        "q over 256 characters",
      );
      await assertRefusedBeforeSending(
        () => api.suggestResources({ type: "job", q: "a\tb" }),
        "q with a control character",
      );
      await assertRefusedBeforeSending(
        () => api.suggestResources({ type: "job", limit: 51 }),
        "limit over 50",
      );
    });

    it("fails closed on a body in the wrong shape", async () => {
      await assertInvalidResponse(() => api.suggestResources({ type: "job" }), {
        items: [],
        truncated: false,
        source: "somewhere_else",
        note: null,
      });
    });
  });

  describe("getMetricsBreakdown", () => {
    it("sends GET /metrics/breakdown with no query string for no query", async () => {
      respond = () => jsonResponse(200, example("MetricsBreakdown"));
      const breakdown = await api.getMetricsBreakdown();
      assert.equal(lastCall().url, `${PREFIX}/metrics/breakdown`);
      assert.equal(lastCall().init.method, "GET");
      assert.deepEqual(
        breakdown,
        api.MetricsBreakdown.parse(example("MetricsBreakdown")),
      );
    });

    it("sends from, to, step and limit in that order, times strictly encoded", async () => {
      respond = () => jsonResponse(200, example("MetricsBreakdown"));
      await api.getMetricsBreakdown({
        from: "2026-11-03T08:00:00Z",
        to: "2026-11-03T09:00:00Z",
        step: "5m",
        limit: 8,
      });
      assert.equal(
        lastCall().url,
        `${PREFIX}/metrics/breakdown?from=2026-11-03T08%3A00%3A00Z&to=2026-11-03T09%3A00%3A00Z&step=5m&limit=8`,
      );
    });

    it("hands the caller's signal to fetch", async () => {
      respond = () => jsonResponse(200, example("MetricsBreakdown"));
      const controller = new AbortController();
      await api.getMetricsBreakdown({}, { signal: controller.signal });
      assert.equal(lastCall().init.signal, controller.signal);
    });

    it("refuses a query the schema refuses, before sending", async () => {
      await assertRefusedBeforeSending(
        () => api.getMetricsBreakdown({ limit: 21 }),
        "limit over 20",
      );
      await assertRefusedBeforeSending(
        () => api.getMetricsBreakdown({ step: "10m" as "1m" }),
        "step outside the list",
      );
      await assertRefusedBeforeSending(
        () => api.getMetricsBreakdown({ from: "today" }),
        "a time that is not ISO UTC",
      );
    });

    it("fails closed on a body in the wrong shape", async () => {
      await assertInvalidResponse(() => api.getMetricsBreakdown(), {
        from: "2026-11-03T08:00:00Z",
        to: "2026-11-03T09:00:00Z",
        step: "1m",
        series: [{ app_slug: "sales" }],
        other: null,
      });
    });
  });
});
