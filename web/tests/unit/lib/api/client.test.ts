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

describe("client", () => {
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

  it("sends same-origin, uncached, JSON-accepting requests and parses the response", async () => {
    respond = () => jsonResponse(200, example("MeResponse"));
    const me = await api.getMe();
    assert.deepEqual(me, api.MeResponse.parse(example("MeResponse")));
    const { url, init } = lastCall();
    assert.equal(url, "/_appmux/api/v1/me");
    assert.equal(init.method, "GET");
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.mode, "same-origin");
    assert.equal(init.cache, "no-store");
    assert.equal(init.body, undefined);
    assert.deepEqual(init.headers, { Accept: "application/json" });
  });

  it("sends a JSON body with its content type, and none without one", async () => {
    respond = () => jsonResponse(200, example("AppDetail"));
    await api.registerApp({
      slug: "sales",
      display_name: "Sales",
      source_path: "/Workspace/Shared/sales",
      memory_budget_mb: 512,
      owner: "alice@example.com",
    });
    const withBody = lastCall();
    assert.equal(withBody.init.method, "POST");
    assert.deepEqual(withBody.init.headers, {
      Accept: "application/json",
      "Content-Type": "application/json",
    });
    assert.deepEqual(JSON.parse(withBody.init.body as string), {
      slug: "sales",
      display_name: "Sales",
      source_path: "/Workspace/Shared/sales",
      memory_budget_mb: 512,
      owner: "alice@example.com",
    });

    respond = () => jsonResponse(200, example("AppStateResponse"));
    await api.restartApp("sales");
    assert.equal(lastCall().init.body, undefined);
    assert.deepEqual(lastCall().init.headers, { Accept: "application/json" });
  });

  it("resolves a 204 route to undefined without reading a body", async () => {
    respond = () => new Response(null, { status: 204 });
    assert.equal(await api.deleteApp("sales"), undefined);
  });

  it("sends the reason and version of a delegation revoke as query parameters, with no body", async () => {
    respond = () => jsonResponse(200, example("AppDetail"));
    const detail = await api.revokeDelegation("sales", "obo", {
      version: 3,
      reason: "needs a+b & 100% \u00e9",
    });
    const { url, init } = lastCall();
    assert.equal(
      url,
      "/_appmux/api/v1/apps/sales/delegation/obo?reason=needs%20a%2Bb%20%26%20100%25%20%C3%A9&version=3",
    );
    assert.equal(init.method, "DELETE");
    assert.equal(init.body, undefined);
    assert.deepEqual(init.headers, { Accept: "application/json" });
    assert.deepEqual(detail, api.AppDetail.parse(example("AppDetail")));
  });

  it("sends the reason and version of a delegation grant as a JSON body and returns the new app", async () => {
    respond = () => jsonResponse(200, example("AppDetail"));
    const body = api.DelegationReasonRequest.parse(
      example("DelegationReasonRequest"),
    );
    const detail = await api.grantDelegation("sales", "shared_sp", body);
    const { url, init } = lastCall();
    assert.equal(url, "/_appmux/api/v1/apps/sales/delegation/shared_sp");
    assert.equal(init.method, "PUT");
    assert.deepEqual(JSON.parse(init.body as string), body);
    assert.equal(
      detail.version,
      api.AppDetail.parse(example("AppDetail")).version,
    );
  });

  it("tells a created release (202) from an up-to-date folder (200) by status", async () => {
    respond = () => jsonResponse(202, example("ReleaseCreated"));
    const created = await api.createRelease("sales");
    assert.equal(created.status, "pending");
    assert.equal(
      created.release_no,
      api.ReleaseCreated.parse(example("ReleaseCreated")).release_no,
    );

    respond = () => jsonResponse(200, example("ReleaseUnchanged"));
    const unchanged = await api.createRelease("sales");
    assert.equal(unchanged.status, "up_to_date");
    assert.equal(
      unchanged.release_no,
      api.ReleaseUnchanged.parse(example("ReleaseUnchanged")).release_no,
    );

    respond = () => jsonResponse(200, { status: "queued", release_no: 1 });
    await assert.rejects(api.createRelease("sales"), (error: unknown) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.code, "invalid_response");
      return true;
    });
  });

  it("sends the apps version with an owner change and a per-app egress edit, and returns the new one", async () => {
    respond = () => jsonResponse(200, example("AppDetail"));
    await api.setOwner(
      "sales",
      api.SetOwnerRequest.parse(example("SetOwnerRequest")),
    );
    assert.deepEqual(
      JSON.parse(lastCall().init.body as string),
      example("SetOwnerRequest"),
    );

    const detail = await api.putAppEgress(
      "sales",
      api.PutAppEgressRequest.parse(example("PutAppEgressRequest")),
    );
    assert.deepEqual(
      JSON.parse(lastCall().init.body as string),
      example("PutAppEgressRequest"),
    );
    assert.equal(
      detail.version,
      api.AppDetail.parse(example("AppDetail")).version,
    );
  });

  it("returns the platform page's apps in the order sent", async () => {
    respond = () => jsonResponse(200, example("PlatformView"));
    const platform = await api.getPlatform();
    assert.deepEqual(
      platform.apps.map((app) => app.slug),
      ["sales", "docs", "reports"],
    );
  });

  it("turns an error body into an ApiError carrying status, code and message", async () => {
    respond = () => jsonResponse(409, example("ErrorBody"));
    await assert.rejects(api.getApp("sales"), (error: unknown) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, 409);
      const body = api.ErrorBody.parse(example("ErrorBody"));
      assert.equal(error.code, body.error.code);
      assert.equal(error.message, body.error.message);
      return true;
    });
  });

  it("gives a non-JSON failure a status-based code and a message that names the status", async () => {
    respond = () => new Response("<html>Bad gateway</html>", { status: 502 });
    await assert.rejects(api.getMe(), (error: unknown) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, 502);
      assert.equal(error.code, "http_502");
      assert.match(error.message, /502/);
      assert.doesNotMatch(error.message, /Bad gateway/);
      return true;
    });
  });

  it("fails closed when a success body does not match its schema", async () => {
    respond = () => jsonResponse(200, { email: "alice@example.com" });
    await assert.rejects(api.getMe(), (error: unknown) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, 200);
      assert.equal(error.code, "invalid_response");
      assert.match(error.message, /groups/);
      return true;
    });
  });

  it("reports a network failure as status 0 and keeps the cause", async () => {
    const cause = new TypeError("Failed to fetch");
    respond = () => Promise.reject(cause);
    await assert.rejects(api.getMe(), (error: unknown) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, 0);
      assert.equal(error.code, "network_error");
      assert.equal(error.cause, cause);
      return true;
    });
  });

  /** Like the real `fetch`: never answers, and rejects with the signal's reason once the caller aborts. */
  function neverAnswers(): Promise<Response> {
    const { signal } = lastCall().init;
    return new Promise((_resolve, reject) => {
      if (!signal) {
        reject(new TypeError("fetch was given no signal"));
        return;
      }
      if (signal.aborted) {
        reject(signal.reason as Error);
        return;
      }
      signal.addEventListener("abort", () => {
        reject(signal.reason as Error);
      });
    });
  }

  it("hands the caller's signal to fetch, and sends none when there is none", async () => {
    respond = () => jsonResponse(200, example("MeResponse"));
    const controller = new AbortController();
    await api.getMe({ signal: controller.signal });
    assert.equal(lastCall().init.signal, controller.signal);

    await api.getMe();
    assert.equal(lastCall().init.signal, undefined);
  });

  it("rethrows an abort as it came, not as a network failure", async () => {
    respond = neverAnswers;
    const controller = new AbortController();
    const pending = api.getMe({ signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, (error: unknown) => {
      assert.ok(
        error === controller.signal.reason,
        `expected the abort reason, got ${String(error)}`,
      );
      return true;
    });
  });

  it("rethrows an AbortError the browser raised on its own, and any failure after the caller aborted", async () => {
    const abortError = new DOMException("The user aborted.", "AbortError");
    respond = () => Promise.reject(abortError);
    await assert.rejects(api.getMe(), (error: unknown) => error === abortError);

    const controller = new AbortController();
    const failure = new TypeError("Failed to fetch");
    respond = () => {
      controller.abort();
      return Promise.reject(failure);
    };
    await assert.rejects(
      api.getMe({ signal: controller.signal }),
      (error: unknown) => error === failure,
    );
  });

  it("rethrows an abort that arrives while the body is read, for a success and for an error answer", async () => {
    for (const status of [200, 500]) {
      const controller = new AbortController();
      respond = () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(stream) {
              controller.signal.addEventListener("abort", () => {
                stream.error(controller.signal.reason);
              });
            },
          }),
          { status },
        );
      const pending = api.getMe({ signal: controller.signal });
      await new Promise((resolve) => setImmediate(resolve));
      controller.abort();
      await assert.rejects(pending, (error: unknown) => {
        assert.ok(
          error === controller.signal.reason,
          `status ${status}: expected the abort reason, got ${String(error)}`,
        );
        return true;
      });
    }
  });

  it("raises the codes errors.ts exports, so a page can branch on a constant instead of a string", async () => {
    respond = () => Promise.reject(new TypeError("Failed to fetch"));
    await assert.rejects(api.getMe(), { code: api.NETWORK_ERROR });

    respond = () => jsonResponse(200, { email: "alice@example.com" });
    await assert.rejects(api.getMe(), { code: api.INVALID_RESPONSE });

    respond = () => new Response("not json", { status: 200 });
    await assert.rejects(api.getMe(), { code: api.INVALID_RESPONSE });

    respond = () => new Response("<html></html>", { status: 502 });
    await assert.rejects(api.getMe(), { code: api.httpStatusCode(502) });
  });

  it("encodes path parameters and refuses ones that could climb out of the route", async () => {
    await api
      .deleteGrant("sales", "user", "a+b@example.com")
      .catch(() => undefined);
    assert.equal(
      lastCall().url,
      "/_appmux/api/v1/apps/sales/grants/user/a%2Bb%40example.com",
    );

    await api
      .getReleaseSourceFile("sales", 7, "src/my app.py")
      .catch(() => undefined);
    assert.equal(
      lastCall().url,
      "/_appmux/api/v1/apps/sales/releases/7/source/src/my%20app.py",
    );

    const callsBefore = calls.length;
    for (const bad of ["..", ".", "", "a/b"]) {
      await assert.rejects(
        api.getApp(bad),
        /path segment/,
        JSON.stringify(bad),
      );
    }
    await assert.rejects(
      api.getReleaseSourceFile("sales", 7, "../secret"),
      /path segment/,
    );
    await assert.rejects(
      api.getReleaseSourceFile("sales", 7, "a//b"),
      /path segment/,
    );
    assert.equal(calls.length, callsBefore, "a refused path sends nothing");
  });

  it("refuses a query the route does not take before it sends anything", async () => {
    const queries: [string, () => Promise<unknown>][] = [
      ["limit over 1000", () => api.listAudit({ limit: 5000 })],
      ["limit 0", () => api.listAudit({ limit: 0 })],
      ["a time that is not ISO UTC", () => api.getMetrics({ from: "today" })],
    ];
    for (const [label, call] of queries) {
      await assert.rejects(call(), (error: unknown) => {
        assert.ok(error instanceof api.ApiError, label);
        assert.equal(error.status, 0, label);
        assert.equal(error.code, api.INVALID_REQUEST, label);
        return true;
      });
    }
    assert.equal(calls.length, 0);
  });

  it("refuses a release number or request id that is not a whole number from 1, and sends nothing", async () => {
    for (const bad of [Number.NaN, 1.5, 0, -1, Number.POSITIVE_INFINITY]) {
      await assert.rejects(api.getRelease("sales", bad), /path segment/);
      await assert.rejects(api.withdrawRequest(bad), /path segment/);
    }
    assert.equal(calls.length, 0);
    await api.getRelease("sales", 1).catch(() => undefined);
    assert.equal(lastCall().url, "/_appmux/api/v1/apps/sales/releases/1");
  });

  it("builds the query string and omits the parameters that are not set", async () => {
    await api.listAudit({ app: "sales", limit: 50 }).catch(() => undefined);
    assert.equal(lastCall().url, "/_appmux/api/v1/audit?app=sales&limit=50");

    await api
      .getMetrics({ app: "_platform", from: "2026-10-30T08:00:00Z" })
      .catch(() => undefined);
    assert.equal(
      lastCall().url,
      "/_appmux/api/v1/metrics?app=_platform&from=2026-10-30T08%3A00%3A00Z",
    );

    await api.getMetrics().catch(() => undefined);
    assert.equal(lastCall().url, "/_appmux/api/v1/metrics");
  });
});
