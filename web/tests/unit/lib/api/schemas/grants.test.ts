/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { accepts, rejects } from "../../../../support/schema-assertions";

describe("grants", () => {
  it("everyone is named * and takes CAN_USE only", () => {
    accepts(api.GrantRequest, {
      type: "everyone",
      name: "*",
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "everyone",
      name: "*",
      permission: "CAN_MANAGE",
    });
    rejects(api.GrantRequest, {
      type: "everyone",
      name: "sales-team",
      permission: "CAN_USE",
    });
  });

  it("a user is a lower-case email, a group is a non-blank name, the type is one of three", () => {
    accepts(api.GrantRequest, {
      type: "user",
      name: "alice@example.com",
      permission: "CAN_MANAGE",
    });
    rejects(api.GrantRequest, {
      type: "user",
      name: "Carol",
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "user",
      name: "Alice@example.com",
      permission: "CAN_USE",
    });
    accepts(api.GrantRequest, {
      type: "group",
      name: "sales-team",
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "group",
      name: " ",
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "group",
      name: "*",
      permission: "CAN_USE",
    });
    accepts(api.GrantRequest, {
      type: "group",
      name: "data team (EMEA)",
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "team",
      name: "x",
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "group",
      name: "x",
      permission: "CAN_EDIT",
    });
  });

  it("a service principal is its lower-case application id and takes CAN_USE only", () => {
    const applicationId = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab";
    accepts(api.GrantRequest, {
      type: "service_principal",
      name: applicationId,
      permission: "CAN_USE",
    });
    rejects(api.GrantRequest, {
      type: "service_principal",
      name: applicationId,
      permission: "CAN_MANAGE",
    });
    for (const name of [
      applicationId.toUpperCase(),
      applicationId.replaceAll("-", ""),
      "sales-bot",
      `${applicationId}\n`,
      "*",
    ]) {
      rejects(api.GrantRequest, {
        type: "service_principal",
        name,
        permission: "CAN_USE",
      });
    }
    accepts(api.AppGrant, {
      type: "service_principal",
      name: applicationId,
      permission: "CAN_USE",
      granted_by: "alice@example.com",
      granted_at: "2026-11-03T10:05:00Z",
    });
  });

  it("refuses a field it does not list", () => {
    rejects(api.GrantRequest, {
      type: "group",
      name: "x",
      permission: "CAN_USE",
      granted_by: "me",
    });
  });
});
