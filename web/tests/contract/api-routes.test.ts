/** The client calls exactly the routes of the contract, one function each. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import * as api from "@/lib/api";
import {
  CONTRACT_DIR,
  EXAMPLES_DIR,
  readJson,
} from "../support/contract-examples";
import {
  jsonResponse,
  type RecordedCall,
  stubFetch,
} from "../support/fake-fetch";
import { manifest } from "../support/manifest";

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

  it("calls exactly the routes of the contract, one function each", async () => {
    const demo = { version: 1, reason: "r" };
    const egress = { rules: [] };
    const appEgress = { version: 1, rules: [] };
    const table: [
      route: string,
      call: (options: api.RequestOptions) => Promise<unknown>,
      url: string,
    ][] = [
      ["GET /me", (options) => api.getMe(options), "/me"],
      ["GET /apps", (options) => api.listApps(options), "/apps"],
      [
        "POST /apps/{app}/open-mode",
        (options) => api.setOpenMode("a", { mode: "as_user" }, options),
        "/apps/a/open-mode",
      ],
      [
        "POST /apps",
        (options) =>
          api.registerApp(
            api.RegisterAppRequest.parse({
              slug: "a",
              display_name: "A",
              source_path: "/Workspace/a",
              memory_budget_mb: 64,
              owner: "o@example.com",
            }),
            options,
          ),
        "/apps",
      ],
      ["GET /apps/{app}", (options) => api.getApp("a", options), "/apps/a"],
      [
        "PATCH /apps/{app}",
        (options) =>
          api.updateApp("a", { version: 1, display_name: "B" }, options),
        "/apps/a",
      ],
      [
        "PUT /apps/{app}/owner",
        (options) =>
          api.setOwner("a", { version: 1, owner: "o@example.com" }, options),
        "/apps/a/owner",
      ],
      [
        "DELETE /apps/{app}",
        (options) => api.deleteApp("a", options),
        "/apps/a",
      ],
      [
        "POST /apps/{app}/start",
        (options) => api.startApp("a", options),
        "/apps/a/start",
      ],
      [
        "POST /apps/{app}/stop",
        (options) => api.stopApp("a", options),
        "/apps/a/stop",
      ],
      [
        "POST /apps/{app}/restart",
        (options) => api.restartApp("a", options),
        "/apps/a/restart",
      ],
      [
        "POST /apps/{app}/releases",
        (options) => api.createRelease("a", options),
        "/apps/a/releases",
      ],
      [
        "GET /apps/{app}/releases/{id}",
        (options) => api.getRelease("a", 3, options),
        "/apps/a/releases/3",
      ],
      [
        "POST /apps/{app}/releases/{id}/activate",
        (options) => api.activateRelease("a", 3, options),
        "/apps/a/releases/3/activate",
      ],
      [
        "PUT /apps/{app}/releases/{id}",
        (options) =>
          api.updateRelease("a", 3, { name: null, tags: [] }, options),
        "/apps/a/releases/3",
      ],
      [
        "PUT /apps/{app}/releases/{id}/lock",
        (options) => api.lockRelease("a", 3, options),
        "/apps/a/releases/3/lock",
      ],
      [
        "DELETE /apps/{app}/releases/{id}/lock",
        (options) => api.unlockRelease("a", 3, options),
        "/apps/a/releases/3/lock",
      ],
      [
        "DELETE /apps/{app}/releases/{id}",
        (options) => api.deleteRelease("a", 3, options),
        "/apps/a/releases/3",
      ],
      [
        "GET /apps/{app}/releases/{id}/source",
        (options) => api.listReleaseSource("a", 3, options),
        "/apps/a/releases/3/source",
      ],
      [
        "GET /apps/{app}/releases/{id}/source/{path}",
        (options) => api.getReleaseSourceFile("a", 3, "x/y.py", options),
        "/apps/a/releases/3/source/x/y.py",
      ],
      [
        "PUT /apps/{app}/config",
        (options) =>
          api.updateConfig(
            "a",
            api.UpdateConfigRequest.parse(example("UpdateConfigRequest")),
            options,
          ),
        "/apps/a/config",
      ],
      [
        "GET /apps/{app}/requirements",
        (options) => api.getRequirements("a", options),
        "/apps/a/requirements",
      ],
      [
        "PUT /apps/{app}/resources/{key}",
        (options) =>
          api.attachResource(
            "a",
            "k",
            {
              type: "secret",
              target: "s/k",
              permission: "READ",
            },
            options,
          ),
        "/apps/a/resources/k",
      ],
      [
        "DELETE /apps/{app}/resources/{key}",
        (options) => api.detachResource("a", "k", options),
        "/apps/a/resources/k",
      ],
      [
        "PUT /apps/{app}/grants",
        (options) =>
          api.putGrant(
            "a",
            {
              type: "everyone",
              name: "*",
              permission: "CAN_USE",
            },
            options,
          ),
        "/apps/a/grants",
      ],
      [
        "DELETE /apps/{app}/grants/{type}/{name}",
        (options) => api.deleteGrant("a", "everyone", "*", options),
        "/apps/a/grants/everyone/*",
      ],
      ["GET /requests", (options) => api.listRequests(options), "/requests"],
      [
        "POST /requests",
        (options) =>
          api.createRequest(
            api.CreatePermissionRequest.parse(
              example("CreatePermissionRequest"),
            ),
            options,
          ),
        "/requests",
      ],
      [
        "POST /requests/{id}/withdraw",
        (options) => api.withdrawRequest(4, options),
        "/requests/4/withdraw",
      ],
      [
        "POST /requests/{id}/approve",
        (options) => api.approveRequest(4, {}, options),
        "/requests/4/approve",
      ],
      [
        "POST /requests/{id}/deny",
        (options) => api.denyRequest(4, { note: "n" }, options),
        "/requests/4/deny",
      ],
      [
        "PUT /apps/{app}/delegation/{kind}",
        (options) => api.grantDelegation("a", "shared_sp", demo, options),
        "/apps/a/delegation/shared_sp",
      ],
      [
        "DELETE /apps/{app}/delegation/{kind}",
        (options) => api.revokeDelegation("a", "obo", demo, options),
        "/apps/a/delegation/obo?reason=r&version=1",
      ],
      ["GET /platform", (options) => api.getPlatform(options), "/platform"],
      [
        "PUT /platform/settings/{key}",
        (options) =>
          api.putSetting("max_releases_per_app", { value: 10 }, options),
        "/platform/settings/max_releases_per_app",
      ],
      [
        "PUT /platform/admins",
        (options) =>
          api.putAdmins(
            {
              admins: [{ type: "user", name: "d@example.com" }],
              reason: "r",
            },
            options,
          ),
        "/platform/admins",
      ],
      [
        "PUT /platform/egress",
        (options) => api.putPlatformEgress(egress, options),
        "/platform/egress",
      ],
      [
        "PUT /apps/{app}/egress",
        (options) => api.putAppEgress("a", appEgress, options),
        "/apps/a/egress",
      ],
      [
        "POST /platform/self-check",
        (options) => api.runSelfCheck(options),
        "/platform/self-check",
      ],
      ["GET /audit", (options) => api.listAudit(undefined, options), "/audit"],
      [
        "GET /metrics",
        (options) => api.getMetrics(undefined, options),
        "/metrics",
      ],
    ];

    const contractRoutes = new Set(manifest.ErrorBody?.routes);
    assert.deepEqual(new Set(table.map(([route]) => route)), contractRoutes);
    assert.equal(table.length, contractRoutes.size, "one function per route");
    assert.deepEqual([...api.API_ROUTES].sort(), [...contractRoutes].sort());

    for (const [route, call, url] of table) {
      calls.length = 0;
      const controller = new AbortController();
      await call({ signal: controller.signal }).catch(() => undefined);
      assert.equal(calls.length, 1, route);
      const [method] = route.split(" ");
      assert.equal(lastCall().init.method, method, route);
      assert.equal(lastCall().url, `/_appmux/api/v1${url}`, route);
      assert.equal(lastCall().init.signal, controller.signal, route);
    }
  });

  it("lists the same routes as the Routes tables of admin-api.md", () => {
    const text = readFileSync(path.join(CONTRACT_DIR, "admin-api.md"), "utf8");
    const routesSection = text.slice(
      text.indexOf("## Routes"),
      text.indexOf("## Schemas"),
    );
    const documented = [
      ...routesSection.matchAll(/^\| `([A-Z]+) (\/[^`?]*)(?:\?[^`]*)?` \|/gm),
    ].map(([, method, route]) => `${method} ${route}`);
    assert.deepEqual([...api.API_ROUTES].sort(), documented.sort());
  });
});
