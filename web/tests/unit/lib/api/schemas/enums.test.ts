/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";
import type { z } from "zod";

import * as api from "@/lib/api";
import { readExampleObject } from "../../../../support/contract-examples";
import {
  accepts,
  rejects,
  rule,
  variant,
} from "../../../../support/schema-assertions";

describe("closed lists", () => {
  const lists: [name: string, schema: z.ZodEnum, expected: string[]][] = [
    [
      "AppState",
      api.AppState,
      ["stopped", "starting", "running", "crashed", "no_release"],
    ],
    ["DesiredState", api.DesiredState, ["running", "stopped"]],
    ["AppPermission", api.AppPermission, ["CAN_USE", "CAN_MANAGE"]],
    ["OpenMode", api.OpenMode, ["as_user", "without_token"]],
    ["Runtime", api.Runtime, ["python", "node"]],
    [
      "ReleaseStatus",
      api.ReleaseStatus,
      [
        "pending",
        "building",
        "starting",
        "healthy",
        "active",
        "failed",
        "superseded",
      ],
    ],
    ["AccessAs", api.AccessAs, ["obo", "own_sp", "shared_sp"]],
    [
      "GrantType",
      api.GrantType,
      ["user", "group", "everyone", "service_principal"],
    ],
    ["DelegationKind", api.DelegationKind, ["obo", "shared_sp"]],
    ["RequestKind", api.RequestKind, ["register", "delegation", "settings"]],
    [
      "RequestStatus",
      api.RequestStatus,
      ["pending", "approved", "denied", "withdrawn"],
    ],
    [
      "PackageMode",
      api.PackageMode,
      ["index", "wheelhouse", "wheelhouse+index"],
    ],
    [
      "SettingKey",
      api.SettingKey,
      [
        "memory_reserve_mb",
        "default_memory_budget_mb",
        "package_mode",
        "default_wheel_sources",
        "shared_sp_delegation_allowed",
        "max_releases_per_app",
        "max_locked_releases",
        "request_timeout_s",
        "max_request_body_mb",
        "max_concurrent_requests",
        "build_timeout_s",
        "max_processes_per_child",
        "child_cpu_pin",
        "attach_check_warehouse_id",
      ],
    ],
  ];

  for (const [name, schema, expected] of lists) {
    it(`${name} holds exactly the values of the contract`, () => {
      assert.deepEqual([...schema.options], expected);
      for (const value of expected) accepts(schema, value, value);
      rejects(schema, "other", "an unlisted value");
      rejects(schema, 1, "a number");
    });
  }

  it("closes the lists written inline in a schema", () => {
    const requirement = { key: "k", offer: null, suggested_type: null };
    for (const state of ["attached", "missing"]) {
      accepts(api.Requirement, { ...requirement, state }, state);
    }
    rejects(api.Requirement, { ...requirement, state: "present" });

    const resource = readExampleObject("ResourceView");
    for (const check_result of ["ok", "unverified"]) {
      accepts(api.ResourceView, variant(resource, { check_result }));
    }
    rejects(api.ResourceView, variant(resource, { check_result: "failed" }));

    const file = (kind: string) => ({
      release_no: 1,
      items: [{ path: "a.py", size_bytes: 1, kind }],
    });
    accepts(api.ReleaseSourceListing, file("text"));
    accepts(api.ReleaseSourceListing, file("binary"));
    rejects(api.ReleaseSourceListing, file("image"));

    accepts(api.ReleaseCreated, { release_no: 1, status: "pending" });
    rejects(api.ReleaseCreated, { release_no: 1, status: "active" });
    accepts(api.ReleaseUnchanged, { release_no: 1, status: "up_to_date" });
    rejects(api.ReleaseUnchanged, { release_no: 1, status: "pending" });

    for (const action of ["allow", "deny"]) {
      accepts(api.EgressRule, { ...rule(), action }, action);
    }
    rejects(api.EgressRule, { ...rule(), action: "drop" });
    for (const type of ["user", "group", "everyone"]) {
      accepts(api.Principal, { type, name: "x" }, type);
    }
    rejects(api.Principal, { type: "team", name: "x" });
  });
});
