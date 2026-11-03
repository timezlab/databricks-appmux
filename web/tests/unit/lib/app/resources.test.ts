import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  accessAsLabel,
  accessModeChoices,
  attachDraftFromOffer,
  attachDraftFromRequirement,
  buildAttachRequest,
  checkResultLabel,
  chooseDraftObject,
  chooseDraftType,
  deriveResourceKey,
  detachWarning,
  editDraftKey,
  emptyAttachDraft,
  findResourceType,
  isScopePrefix,
  requirementRows,
  requirementsSummary,
  resourceLevelLabel,
  resourceTypeLabel,
  typeDraftTarget,
  unusedResourceNote,
} from "@/lib/app/resources";
import type { AttachDraft, ResourceCatalogue } from "@/lib/app/resources";
import { readExample } from "../../../support/contract-examples";

const APP = api.AppDetail.parse(readExample("AppDetail"));
const TAKEN = APP.resources.map((resource) => resource.key);

function appWith(overrides: Partial<api.AppDetail>): api.AppDetail {
  return { ...APP, ...overrides };
}

const EXAMPLE_TYPES = api.ResourceTypeList.parse(
  readExample("ResourceTypeList"),
).items;

function entry(
  type: string,
  overrides: Partial<api.ResourceTypeEntry> = {},
): api.ResourceTypeEntry {
  return {
    type,
    label: type.replaceAll("_", " "),
    description: `A ${type}.`,
    permissions: ["CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"],
    access_as: ["obo", "own_sp"],
    target_hint: "my-object",
    can_suggest: false,
    ...overrides,
  };
}

// The contract's two examples (secret, SQL warehouse), plus a type without an
// attach check and one that cannot be listed.
const CATALOGUE: ResourceCatalogue = [
  ...EXAMPLE_TYPES,
  entry("serving_endpoint", {
    label: "serving endpoint",
    can_suggest: true,
  }),
  entry("uc_table", { permissions: ["SELECT", "MODIFY"] }),
];

function typeOf(type: string): api.ResourceTypeEntry {
  const found = findResourceType(CATALOGUE, type);
  if (found === null) throw new Error(`no ${type} in the test catalogue`);
  return found;
}

const WAREHOUSE = typeOf("sql_warehouse");
const SECRET = typeOf("secret");
const ENDPOINT = typeOf("serving_endpoint");

const OFFER_WAREHOUSE: api.BundleResource = {
  key: "sql-warehouse",
  type: "sql_warehouse",
  target: "a1b2c3d4e5f67890",
  permission: "CAN_USE",
};

describe("the catalogue, as data", () => {
  it("names a type by the catalogue's noun, capitalised, and an unknown one by its wire value", () => {
    assert.equal(
      resourceTypeLabel(CATALOGUE, "sql_warehouse"),
      "SQL warehouse",
    );
    assert.equal(resourceTypeLabel(CATALOGUE, "secret"), "Secret");
    assert.equal(
      resourceTypeLabel(CATALOGUE, "quantum_store"),
      "quantum_store",
    );
    assert.equal(resourceTypeLabel(null, "secret"), "secret");
  });

  it("says a level in words", () => {
    assert.equal(resourceLevelLabel("CAN_USE"), "Can use");
    assert.equal(resourceLevelLabel("CAN_MANAGE_RUN"), "Can manage run");
    assert.equal(resourceLevelLabel("READ"), "Read");
  });

  it("treats a secret suggestion ending in / as a scope to go on typing from", () => {
    const scope = {
      target: "sales-secrets/",
      label: "sales-secrets",
      detail: null,
      permissions: ["READ"],
    };
    assert.equal(isScopePrefix("secret", scope), true);
    assert.equal(
      isScopePrefix("secret", { ...scope, target: "sales-secrets/token" }),
      false,
    );
    assert.equal(isScopePrefix("sql_warehouse", scope), false);
  });
});

describe("access modes per type", () => {
  function byValue(app: api.AppDetail, type: api.ResourceTypeEntry) {
    return Object.fromEntries(
      accessModeChoices(app, type).map((choice) => [choice.value, choice]),
    );
  }

  it("offers what the type allows and the app has, in the order visitor, own, shared", () => {
    const choices = accessModeChoices(APP, WAREHOUSE);
    assert.deepEqual(
      choices.map((choice) => [choice.value, choice.isAvailable]),
      [
        ["obo", true],
        ["own_sp", true],
        ["shared_sp", false],
      ],
    );
  });

  it("says the platform switched the shared service principal off, with nothing to ask for", () => {
    const sharedSp = byValue(APP, WAREHOUSE).shared_sp;
    assert.match(sharedSp?.reason ?? "", /switched .* off/i);
    assert.equal(sharedSp?.howToGet, null);
  });

  it("points to the delegation request when the shared service principal can be asked for", () => {
    const askable = appWith({
      delegation: { ...APP.delegation, shared_sp_delegation_allowed: true },
    });
    const sharedSp = byValue(askable, WAREHOUSE).shared_sp;
    assert.equal(sharedSp?.isAvailable, false);
    assert.match(sharedSp?.howToGet ?? "", /Request a delegation/);
  });

  it("offers the shared service principal when it is delegated and the type has a check", () => {
    const delegated = appWith({
      delegation: { ...APP.delegation, shared_sp_delegated: true },
    });
    assert.equal(byValue(delegated, WAREHOUSE).shared_sp?.isAvailable, true);
    // A type without an attach check never offers it, even when delegated.
    const endpoint = byValue(delegated, ENDPOINT).shared_sp;
    assert.equal(endpoint?.isAvailable, false);
    assert.match(endpoint?.reason ?? "", /attach check/);
  });

  it("keeps obo selectable without the delegation and says what the visitor's token needs", () => {
    // Setting is not use: the delegation only decides whether a visitor's token reaches the app.
    const undelegated = appWith({
      delegation: { ...APP.delegation, obo_delegated: false },
    });
    const obo = byValue(undelegated, WAREHOUSE).obo;
    assert.equal(obo?.isAvailable, true);
    assert.equal(obo?.reason, null);
    assert.match(obo?.note ?? "", /until a platform admin delegates OBO/i);
    assert.match(obo?.howToGet ?? "", /Request a delegation/);
    assert.equal(byValue(APP, WAREHOUSE).obo?.note, null);
  });

  it("needs the own-SP keys for own_sp, saying how to get them", () => {
    const bare = appWith({
      config: {
        ...APP.config,
        own_sp_client_id_key: null,
        own_sp_client_secret_key: null,
      },
    });
    const ownSp = byValue(bare, WAREHOUSE).own_sp;
    assert.equal(ownSp?.isAvailable, false);
    assert.match(ownSp?.howToGet ?? "", /client secret/);
    assert.equal(ownSp?.note, null);
  });

  it("names the identity in words, and a secret as read by appmux", () => {
    assert.match(accessAsLabel("obo"), /visitor/i);
    assert.match(accessAsLabel("own_sp"), /own/i);
    assert.match(accessAsLabel("shared_sp"), /shared/i);
    assert.match(accessAsLabel(null), /appmux/i);
  });

  it("says what a stored check result means", () => {
    assert.match(checkResultLabel("ok"), /verified|reached/i);
    assert.match(checkResultLabel("unverified"), /not verified/i);
  });
});

describe("deriveResourceKey", () => {
  it("makes a key of the object's name, else of the type", () => {
    assert.equal(
      deriveResourceKey("sql_warehouse", "Shared Serverless", []),
      "shared-serverless",
    );
    assert.equal(deriveResourceKey("sql_warehouse", null, []), "sql-warehouse");
    assert.equal(
      deriveResourceKey("secret", "openai-api-key", []),
      "openai-api-key",
    );
  });

  it("keeps to the contract's form: lower case, no accents, starting with a letter or digit, at most 63", () => {
    assert.equal(
      deriveResourceKey("job", "  Café Report (v2)! ", []),
      "cafe-report-v2",
    );
    assert.equal(deriveResourceKey("job", "__nightly", []), "nightly");
    const long = deriveResourceKey("job", "x".repeat(80), []);
    assert.equal(long.length, 63);
    for (const key of ["shared-serverless", "cafe-report-v2", long]) {
      assert.ok(api.ResourceKey.safeParse(key).success, key);
    }
    // Nothing usable in the name: the type stands in.
    assert.equal(deriveResourceKey("uc_table", "***", []), "uc-table");
  });

  it("is unique among the app's keys", () => {
    assert.equal(
      deriveResourceKey("sql_warehouse", null, TAKEN),
      "sql-warehouse-2",
    );
    assert.equal(
      deriveResourceKey("sql_warehouse", null, [...TAKEN, "sql-warehouse-2"]),
      "sql-warehouse-3",
    );
    const long = deriveResourceKey("job", "y".repeat(80), ["y".repeat(63)]);
    assert.equal(long, `${"y".repeat(61)}-2`);
    assert.ok(api.ResourceKey.safeParse(long).success);
  });
});

describe("the Add draft", () => {
  it("starts with nothing chosen", () => {
    assert.deepEqual(emptyAttachDraft(), {
      key: "",
      isKeyEdited: false,
      type: "",
      target: "",
      objectName: null,
      permission: "",
      accessAs: "",
      fromBundle: false,
    });
  });

  it("takes the type's first level and a key from the type, and keeps an access mode the type allows", () => {
    const draft = chooseDraftType(
      { ...emptyAttachDraft(), accessAs: "obo" },
      WAREHOUSE,
      TAKEN,
    );
    assert.equal(draft.type, "sql_warehouse");
    assert.equal(draft.permission, "CAN_USE");
    assert.equal(draft.accessAs, "obo");
    assert.equal(draft.key, "sql-warehouse-2");

    const secret = chooseDraftType(draft, SECRET, TAKEN);
    assert.equal(secret.permission, "READ");
    assert.equal(secret.accessAs, "");
    assert.equal(secret.key, "secret");
    assert.equal(secret.target, "");
  });

  it("fills the target and the key from a chosen object, keeping a level the object allows", () => {
    const typed = chooseDraftType(emptyAttachDraft(), WAREHOUSE, TAKEN);
    const draft = chooseDraftObject(
      { ...typed, permission: "CAN_MANAGE" },
      {
        target: "1234567890abcdef",
        label: "Shared Serverless",
        detail: "RUNNING",
        permissions: ["CAN_USE", "CAN_MANAGE"],
      },
      TAKEN,
    );
    assert.equal(draft.target, "1234567890abcdef");
    assert.equal(draft.key, "shared-serverless");
    assert.equal(draft.permission, "CAN_MANAGE");
  });

  it("names a typed secret by its key, and any other typed target by the type", () => {
    const secret = typeDraftTarget(
      chooseDraftType(emptyAttachDraft(), SECRET, TAKEN),
      "sales-secrets/Stripe Key",
      TAKEN,
    );
    assert.equal(secret.key, "stripe-key");
    const warehouse = typeDraftTarget(
      chooseDraftType(emptyAttachDraft(), WAREHOUSE, []),
      "a1b2c3",
      [],
    );
    assert.equal(warehouse.key, "sql-warehouse");
  });

  it("stops following the type and the object once the key is typed", () => {
    const edited = editDraftKey(
      chooseDraftType(emptyAttachDraft(), WAREHOUSE, TAKEN),
      "main-warehouse",
    );
    const next = chooseDraftObject(
      edited,
      {
        target: "fedcba0987654321",
        label: "Reporting",
        detail: null,
        permissions: ["CAN_USE", "CAN_MANAGE"],
      },
      TAKEN,
    );
    assert.equal(next.key, "main-warehouse");
    assert.equal(next.isKeyEdited, true);
  });

  it("is filled from a bundle offer, keeping the key the release refers to", () => {
    const draft = attachDraftFromOffer(OFFER_WAREHOUSE, CATALOGUE);
    assert.equal(draft.key, "sql-warehouse");
    assert.equal(draft.isKeyEdited, true);
    assert.equal(draft.target, "a1b2c3d4e5f67890");
    assert.equal(draft.permission, "CAN_USE");
    assert.equal(draft.fromBundle, true);
  });

  it("leaves an unresolved target and a level the type does not have empty", () => {
    const draft = attachDraftFromOffer(
      {
        key: "forecast-model",
        type: "serving_endpoint",
        target: null,
        permission: "IS_OWNER",
      },
      CATALOGUE,
    );
    assert.equal(draft.target, "");
    assert.equal(draft.permission, "");
  });

  it("starts a missing requirement from its key and the suggested type, or the offer when there is one", () => {
    const suggested = attachDraftFromRequirement(
      {
        key: "main-warehouse",
        state: "missing",
        offer: null,
        suggested_type: "sql_warehouse",
      },
      CATALOGUE,
    );
    assert.equal(suggested.key, "main-warehouse");
    assert.equal(suggested.type, "sql_warehouse");
    assert.equal(suggested.permission, "CAN_USE");
    const offered = attachDraftFromRequirement(
      {
        key: "sql-warehouse",
        state: "missing",
        offer: OFFER_WAREHOUSE,
        suggested_type: "job",
      },
      CATALOGUE,
    );
    assert.equal(offered.fromBundle, true);
    // A suggested type the catalogue does not list leaves the type to choose.
    assert.equal(
      attachDraftFromRequirement(
        { key: "x", state: "missing", offer: null, suggested_type: "quantum" },
        CATALOGUE,
      ).type,
      "",
    );
  });
});

describe("buildAttachRequest", () => {
  const filled: AttachDraft = {
    ...emptyAttachDraft(),
    key: "main-warehouse",
    isKeyEdited: true,
    type: "sql_warehouse",
    target: " a1b2c3d4e5f67890 ",
    permission: "CAN_USE",
    accessAs: "obo",
  };

  it("builds the key and the body, trimmed, and passes the schema", () => {
    const result = buildAttachRequest(APP, filled, WAREHOUSE);
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
    const result = buildAttachRequest(
      APP,
      { ...filled, fromBundle: true },
      WAREHOUSE,
    );
    assert.ok(result.ok);
    assert.equal(result.body.request.from_bundle, true);
  });

  it("builds a secret as a reference with no access mode", () => {
    const result = buildAttachRequest(
      APP,
      {
        ...filled,
        key: "new-secret",
        type: "secret",
        target: "sales-secrets/other-key",
        permission: "READ",
        accessAs: "",
      },
      SECRET,
    );
    assert.ok(result.ok);
    assert.deepEqual(result.body.request, {
      type: "secret",
      target: "sales-secrets/other-key",
      permission: "READ",
    });
  });

  it("explains a secret target that is not scope/key, and warns against typing the value", () => {
    const result = buildAttachRequest(
      APP,
      {
        ...filled,
        key: "new-secret",
        type: "secret",
        target: "justavalue",
        permission: "READ",
        accessAs: "",
      },
      SECRET,
    );
    assert.ok(!result.ok);
    assert.match(result.errors.target ?? "", /scope\/key/);
    assert.match(result.errors.target ?? "", /never/i);
  });

  it("needs a key of the contract's form, not one already attached", () => {
    for (const bad of ["", "Has Space", "UPPER", "x".repeat(64)]) {
      const result = buildAttachRequest(
        APP,
        { ...filled, key: bad },
        WAREHOUSE,
      );
      assert.ok(!result.ok, bad);
      assert.ok(result.errors.key, bad);
    }
    const taken = buildAttachRequest(
      APP,
      { ...filled, key: "sql-warehouse" },
      WAREHOUSE,
    );
    assert.ok(!taken.ok);
    assert.match(taken.errors.key ?? "", /already.*Detach/);
  });

  it("needs a type, a target with the type's example, a level and an access mode", () => {
    const noType = buildAttachRequest(APP, emptyAttachDraft(), null);
    assert.ok(!noType.ok);
    assert.equal(noType.errors.type, "Choose a type.");

    const result = buildAttachRequest(
      APP,
      { ...filled, target: " ", permission: "", accessAs: "" },
      WAREHOUSE,
    );
    assert.ok(!result.ok);
    assert.match(result.errors.target ?? "", /1234567890abcdef/);
    assert.equal(result.errors.permission, "Choose a permission.");
    assert.equal(result.errors.accessAs, "Choose how the app reaches it.");
  });

  it("refuses a level the type does not have, in words", () => {
    const result = buildAttachRequest(
      APP,
      { ...filled, permission: "READ" },
      WAREHOUSE,
    );
    assert.ok(!result.ok);
    assert.equal(result.errors.permission, "Use one of Can use, Can manage.");
  });

  it("refuses an access mode the app cannot use, with its reason, as the server would", () => {
    const result = buildAttachRequest(
      APP,
      { ...filled, accessAs: "shared_sp" },
      WAREHOUSE,
    );
    assert.ok(!result.ok);
    assert.match(result.errors.accessAs ?? "", /switched .* off/i);
  });

  it("refuses a type the catalogue does not offer", () => {
    const result = buildAttachRequest(
      APP,
      { ...filled, type: "quantum_store" },
      null,
    );
    assert.ok(!result.ok);
    assert.match(result.errors.type ?? "", /quantum_store/);
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

  it("notes an attached resource nothing reads, but not the own service principal's keys", () => {
    assert.equal(
      unusedResourceNote(APP, "forecast-model"),
      "Release 8 does not refer to it.",
    );
    assert.equal(unusedResourceNote(APP, "sql-warehouse"), null);
    assert.equal(unusedResourceNote(APP, "sp-client-id"), null);
    assert.equal(
      unusedResourceNote(
        appWith({ requirements: { ...APP.requirements, release_no: null } }),
        "forecast-model",
      ),
      null,
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
