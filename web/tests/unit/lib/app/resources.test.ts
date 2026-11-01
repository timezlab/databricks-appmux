import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  RESOURCE_TYPES,
  accessAsChoices,
  accessAsLabel,
  attachDraftFromOffer,
  attachDraftFromRequirement,
  buildAttachRequest,
  changeDraftType,
  checkResultLabel,
  detachWarning,
  emptyAttachDraft,
  requirementRows,
  requirementsSummary,
  resourceTypeInfo,
  resourceTypeLabel,
} from "@/lib/app/resources";
import { readExample } from "../../../support/contract-examples";

const APP = api.AppDetail.parse(readExample("AppDetail"));

function appWith(overrides: Partial<api.AppDetail>): api.AppDetail {
  return { ...APP, ...overrides };
}

const OFFER_WAREHOUSE: api.BundleResource = {
  key: "sql-warehouse",
  type: "sql_warehouse",
  target: "a1b2c3d4e5f67890",
  permission: "CAN_USE",
};

describe("the resource catalogue (data model 4.7)", () => {
  it("has the fourteen types, each once, and a level for every one", () => {
    assert.equal(RESOURCE_TYPES.length, 14);
    assert.equal(new Set(RESOURCE_TYPES.map((info) => info.type)).size, 14);
    for (const info of RESOURCE_TYPES) {
      assert.ok(info.levels.length >= 1, info.type);
      assert.ok(info.label.length > 0, info.type);
      assert.ok(info.targetLabel.length > 0, info.type);
    }
  });

  it("makes a secret the one type without access_as, and READ its one level", () => {
    const secret = resourceTypeInfo("secret");
    assert.ok(secret);
    assert.deepEqual(secret.levels, ["READ"]);
    assert.equal(secret.hasAccessAs, false);
    for (const info of RESOURCE_TYPES.filter(
      (item) => item.type !== "secret",
    )) {
      assert.equal(info.hasAccessAs, true, info.type);
    }
  });

  it("knows every type the contract's example app uses", () => {
    for (const resource of APP.resources) {
      const info = resourceTypeInfo(resource.type);
      assert.ok(info, resource.type);
      assert.ok(info.levels.includes(resource.permission), resource.key);
    }
  });

  it("shows a type it does not know as it is", () => {
    assert.equal(resourceTypeInfo("quantum_store"), null);
    assert.equal(resourceTypeLabel("quantum_store"), "quantum_store");
    assert.equal(resourceTypeLabel("sql_warehouse"), "SQL warehouse");
  });
});

describe("access_as", () => {
  it("offers only what the app's delegations and own-SP keys allow, with the reason for the rest", () => {
    const choices = accessAsChoices(APP);
    const byValue = Object.fromEntries(
      choices.map((choice) => [choice.value, choice]),
    );
    assert.equal(byValue.obo?.isAvailable, true);
    assert.equal(byValue.own_sp?.isAvailable, true);
    assert.equal(byValue.shared_sp?.isAvailable, false);
    assert.match(byValue.shared_sp?.reason ?? "", /request/i);
  });

  it("needs own-SP keys for own_sp, and OBO delegation for obo", () => {
    const bare = appWith({
      delegation: { ...APP.delegation, obo_delegated: false },
      config: {
        ...APP.config,
        own_sp_client_id_key: null,
        own_sp_client_secret_key: null,
      },
    });
    const choices = accessAsChoices(bare);
    assert.deepEqual(
      choices.map((choice) => [choice.value, choice.isAvailable]),
      [
        ["obo", false],
        ["own_sp", false],
        ["shared_sp", false],
      ],
    );
    assert.match(choices[1]?.reason ?? "", /service principal|keys/i);
  });

  it("offers shared_sp when it is delegated", () => {
    const delegated = appWith({
      delegation: { ...APP.delegation, shared_sp_delegated: true },
    });
    assert.equal(
      accessAsChoices(delegated).find((choice) => choice.value === "shared_sp")
        ?.isAvailable,
      true,
    );
  });

  it("names the identity in words, and a secret as read by appmux", () => {
    assert.match(accessAsLabel("obo"), /visitor/i);
    assert.match(accessAsLabel("own_sp"), /own/i);
    assert.match(accessAsLabel("shared_sp"), /appmux/i);
    assert.match(accessAsLabel(null), /appmux/i);
  });

  it("says what a stored check result means", () => {
    assert.match(checkResultLabel("ok"), /verified|reached/i);
    assert.match(checkResultLabel("unverified"), /not verified/i);
  });
});

describe("attach drafts", () => {
  it("starts empty with the first type and its first level", () => {
    const draft = emptyAttachDraft();
    assert.equal(draft.key, "");
    assert.equal(draft.target, "");
    assert.equal(draft.accessAs, "");
    assert.equal(draft.fromBundle, false);
    assert.equal(
      draft.permission,
      resourceTypeInfo(draft.type)?.levels[0] ?? "",
    );
  });

  it("is filled from a bundle offer, and remembers it came from the file", () => {
    assert.deepEqual(attachDraftFromOffer(OFFER_WAREHOUSE), {
      key: "sql-warehouse",
      type: "sql_warehouse",
      target: "a1b2c3d4e5f67890",
      permission: "CAN_USE",
      accessAs: "",
      fromBundle: true,
    });
  });

  it("leaves the target empty when the file did not resolve it, and the level empty when it is not a level of the type", () => {
    const draft = attachDraftFromOffer({
      key: "forecast-model",
      type: "serving_endpoint",
      target: null,
      permission: "IS_OWNER",
    });
    assert.equal(draft.target, "");
    assert.equal(draft.permission, "");
  });

  it("starts a missing requirement from its key and the suggested type", () => {
    const draft = attachDraftFromRequirement({
      key: "main-warehouse",
      state: "missing",
      offer: null,
      suggested_type: "sql_warehouse",
    });
    assert.equal(draft.key, "main-warehouse");
    assert.equal(draft.type, "sql_warehouse");
    assert.equal(draft.fromBundle, false);
  });

  it("prefers the offer over the suggestion", () => {
    const draft = attachDraftFromRequirement({
      key: "sql-warehouse",
      state: "missing",
      offer: OFFER_WAREHOUSE,
      suggested_type: "job",
    });
    assert.equal(draft.type, "sql_warehouse");
    assert.equal(draft.target, "a1b2c3d4e5f67890");
    assert.equal(draft.fromBundle, true);
  });

  it("changing the type keeps a level that still fits, otherwise takes the first, and drops access_as for a secret", () => {
    const draft = {
      ...emptyAttachDraft(),
      type: "sql_warehouse",
      permission: "CAN_MANAGE",
      accessAs: "obo" as const,
    };
    assert.equal(
      changeDraftType(draft, "serving_endpoint").permission,
      "CAN_MANAGE",
    );
    assert.equal(changeDraftType(draft, "job").permission, "CAN_MANAGE");
    assert.equal(changeDraftType(draft, "uc_table").permission, "SELECT");
    const secret = changeDraftType(draft, "secret");
    assert.equal(secret.permission, "READ");
    assert.equal(secret.accessAs, "");
  });
});

describe("buildAttachRequest", () => {
  const filled = {
    key: "main-warehouse",
    type: "sql_warehouse",
    target: " a1b2c3d4e5f67890 ",
    permission: "CAN_USE",
    accessAs: "obo" as const,
    fromBundle: false,
  };

  it("builds the key and the body, trimmed, and passes the schema", () => {
    const result = buildAttachRequest(APP, filled);
    assert.ok(result.ok);
    assert.equal(result.body.key, "main-warehouse");
    assert.deepEqual(result.body.request, {
      type: "sql_warehouse",
      target: "a1b2c3d4e5f67890",
      permission: "CAN_USE",
      access_as: "obo",
    });
  });

  it("records an accepted offer", () => {
    const result = buildAttachRequest(APP, { ...filled, fromBundle: true });
    assert.ok(result.ok);
    assert.equal(result.body.request.from_bundle, true);
  });

  it("builds a secret as a reference with no access_as", () => {
    const result = buildAttachRequest(APP, {
      key: "new-secret",
      type: "secret",
      target: "sales-secrets/other-key",
      permission: "READ",
      accessAs: "",
      fromBundle: false,
    });
    assert.ok(result.ok);
    assert.deepEqual(result.body.request, {
      type: "secret",
      target: "sales-secrets/other-key",
      permission: "READ",
    });
  });

  it("explains a secret target that is not scope/key, and warns against typing the value", () => {
    const result = buildAttachRequest(APP, {
      ...filled,
      key: "new-secret",
      type: "secret",
      target: "justavalue",
      permission: "READ",
      accessAs: "",
    });
    assert.ok(!result.ok);
    assert.match(result.errors.target ?? "", /scope\/key/);
    assert.match(result.errors.target ?? "", /never/i);
  });

  it("needs a key of the contract's form", () => {
    for (const bad of ["", "Has Space", "UPPER", "x".repeat(64)]) {
      const result = buildAttachRequest(APP, { ...filled, key: bad });
      assert.ok(!result.ok, bad);
      assert.ok(result.errors.key, bad);
    }
  });

  it("refuses a key that is already attached, and says to detach first", () => {
    const result = buildAttachRequest(APP, { ...filled, key: "sql-warehouse" });
    assert.ok(!result.ok);
    assert.match(result.errors.key ?? "", /already/i);
    assert.match(result.errors.key ?? "", /detach/i);
  });

  it("needs a target and, except for a secret, an identity", () => {
    const result = buildAttachRequest(APP, {
      ...filled,
      target: " ",
      accessAs: "",
    });
    assert.ok(!result.ok);
    assert.ok(result.errors.target);
    assert.ok(result.errors.accessAs);
  });

  it("refuses a level that is not one of the type", () => {
    const result = buildAttachRequest(APP, { ...filled, permission: "READ" });
    assert.ok(!result.ok);
    assert.match(result.errors.permission ?? "", /CAN_USE/);
  });

  it("refuses an identity the app cannot use, as the server would", () => {
    const result = buildAttachRequest(APP, {
      ...filled,
      accessAs: "shared_sp",
    });
    assert.ok(!result.ok);
    assert.match(result.errors.accessAs ?? "", /request/i);
  });

  it("refuses a type outside the catalogue", () => {
    const result = buildAttachRequest(APP, {
      ...filled,
      type: "quantum_store",
    });
    assert.ok(!result.ok);
    assert.ok(result.errors.type);
  });
});

describe("requirements", () => {
  const requirements: api.RequirementsResponse = {
    release_no: 8,
    items: [
      {
        key: "sql-warehouse",
        state: "attached",
        offer: OFFER_WAREHOUSE,
        suggested_type: null,
      },
      { key: "token", state: "missing", offer: null, suggested_type: "secret" },
    ],
    unreferenced_offers: [
      { key: "spare", type: "job", target: "42", permission: "CAN_VIEW" },
    ],
  };

  it("joins each requirement with the resource that fills it", () => {
    const rows = requirementRows(APP.resources, requirements);
    assert.deepEqual(
      rows.map((row) => [row.key, row.state, row.resource?.key ?? null]),
      [
        ["sql-warehouse", "attached", "sql-warehouse"],
        ["token", "missing", null],
      ],
    );
  });

  it("counts attached and missing", () => {
    assert.deepEqual(requirementsSummary(requirements), {
      attached: 1,
      missing: 1,
      text: "1 of 2 attached",
    });
    assert.equal(
      requirementsSummary({ ...requirements, items: [] }).text,
      "Nothing is required",
    );
  });
});

describe("detachWarning", () => {
  it("names the env entries that read the resource", () => {
    const app = appWith({
      config: {
        ...APP.config,
        env: [
          { name: "WAREHOUSE_ID", value_from: "sql-warehouse" },
          { name: "LOG_LEVEL", value: "info" },
        ],
      },
    });
    const text = detachWarning(app, "sql-warehouse");
    assert.match(text ?? "", /WAREHOUSE_ID/);
    assert.doesNotMatch(text ?? "", /LOG_LEVEL/);
  });

  it("warns when the key holds the app's own service principal", () => {
    const text = detachWarning(APP, "sp-client-id");
    assert.match(text ?? "", /service principal/i);
  });

  it("has nothing to add for a resource nothing reads", () => {
    assert.equal(detachWarning(APP, "experiment"), null);
  });
});
