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

  it("refuses a field it does not list", () => {
    rejects(api.GrantRequest, {
      type: "group",
      name: "x",
      permission: "CAN_USE",
      granted_by: "me",
    });
  });
});
