/**
 * The app's resources and requirements (design 8.2, data model 4.7). The
 * catalogue of resource types lives in code, as it does on the server; a type
 * the server adds later is shown by its raw name until this list learns it.
 * The attach form checks what the contract can check from the page's data and
 * leaves the attach check itself (the attacher's own token reaching the
 * target) to the server.
 */
import { AttachResourceRequest, ResourceKey } from "@/lib/api";
import type {
  AccessAs,
  AppDetail,
  BundleResource,
  Requirement,
  RequirementsResponse,
  ResourceView,
} from "@/lib/api";
import { FORM_ERROR_KEY, errorsFromZod, formFailure } from "./forms";
import type { FormErrors, FormResult } from "./forms";

// ---------------------------------------------------------------- catalogue

export interface ResourceTypeInfo {
  /** The value of `type` on the wire. */
  type: string;
  label: string;
  /** What the target is called in the form ("Warehouse id"). */
  targetLabel: string;
  /** Permission levels of the type, in the order the form lists them. */
  levels: readonly string[];
  /** False only for a secret, which appmux reads itself at start. */
  hasAccessAs: boolean;
}

function typeInfo(
  type: string,
  label: string,
  targetLabel: string,
  levels: readonly string[],
): ResourceTypeInfo {
  return { type, label, targetLabel, levels, hasAccessAs: type !== "secret" };
}

// Data model 4.7. The order is the order of the form's type list.
export const RESOURCE_TYPES: readonly ResourceTypeInfo[] = [
  typeInfo("sql_warehouse", "SQL warehouse", "Warehouse id", [
    "CAN_USE",
    "CAN_MANAGE",
  ]),
  typeInfo("secret", "Secret", "Secret reference (scope/key)", ["READ"]),
  typeInfo("mlflow_experiment", "MLflow experiment", "Experiment id", [
    "CAN_READ",
    "CAN_EDIT",
    "CAN_MANAGE",
  ]),
  typeInfo("serving_endpoint", "Model serving endpoint", "Endpoint name", [
    "CAN_VIEW",
    "CAN_QUERY",
    "CAN_MANAGE",
  ]),
  typeInfo("lakebase_database", "Lakebase database", "Endpoint path or host", [
    "CAN_CONNECT_AND_CREATE",
  ]),
  typeInfo("job", "Lakeflow job", "Job id", [
    "CAN_VIEW",
    "CAN_MANAGE_RUN",
    "CAN_MANAGE",
  ]),
  typeInfo("uc_table", "Unity Catalog table", "Table (catalog.schema.table)", [
    "SELECT",
    "MODIFY",
  ]),
  typeInfo(
    "uc_volume",
    "Unity Catalog volume",
    "Volume path (/Volumes/catalog/schema/volume)",
    ["READ_VOLUME", "WRITE_VOLUME"],
  ),
  typeInfo(
    "uc_function",
    "Unity Catalog function",
    "Function (catalog.schema.function)",
    ["EXECUTE"],
  ),
  typeInfo("uc_connection", "Unity Catalog connection", "Connection name", [
    "USE_CONNECTION",
  ]),
  typeInfo("genie_space", "Genie space", "Space id", [
    "CAN_VIEW",
    "CAN_RUN",
    "CAN_EDIT",
    "CAN_MANAGE",
  ]),
  typeInfo(
    "vector_search_index",
    "AI Search index",
    "Index (catalog.schema.index)",
    ["SELECT"],
  ),
  typeInfo("databricks_app", "Databricks app", "App name", ["CAN_USE"]),
  typeInfo(
    "gateway_model_service",
    "Unity Gateway model service",
    "Service (catalog.schema.service)",
    ["EXECUTE"],
  ),
];

export function resourceTypeInfo(type: string): ResourceTypeInfo | null {
  return RESOURCE_TYPES.find((info) => info.type === type) ?? null;
}

/** The label of a known type, or the wire value of one this page does not know. */
export function resourceTypeLabel(type: string): string {
  return resourceTypeInfo(type)?.label ?? type;
}

// --------------------------------------------------------------- identities

export interface AccessAsChoice {
  value: AccessAs;
  label: string;
  isAvailable: boolean;
  /** Why it cannot be chosen; `null` when it can. */
  reason: string | null;
}

const ACCESS_AS_LABEL: Record<AccessAs, string> = {
  obo: "As the visitor (OBO)",
  own_sp: "As the app's own service principal",
  shared_sp: "As the appmux service principal",
};

/** How the child reaches a resource; a secret has no identity, appmux reads it at start. */
export function accessAsLabel(accessAs: AccessAs | null): string {
  return accessAs === null
    ? "Read by appmux when the app starts"
    : ACCESS_AS_LABEL[accessAs];
}

/** The three identities, each with whether this app can use it now (design 8.2). */
export function accessAsChoices(app: AppDetail): AccessAsChoice[] {
  const { obo_delegated, shared_sp_delegated, shared_sp_delegation_allowed } =
    app.delegation;
  const hasOwnSpKeys =
    app.config.own_sp_client_id_key !== null &&
    app.config.own_sp_client_secret_key !== null;

  let sharedSpReason: string | null = null;
  if (!shared_sp_delegated) {
    sharedSpReason = shared_sp_delegation_allowed
      ? "Not delegated to this app. Ask for shared service principal delegation with a request."
      : "The platform has switched shared service principal delegation off, so a request for it is refused.";
  }

  return [
    choice(
      "obo",
      obo_delegated,
      "Not delegated to this app. Ask for OBO delegation with a request.",
    ),
    choice(
      "own_sp",
      hasOwnSpKeys,
      "The app has no own service principal keys configured.",
    ),
    choice("shared_sp", shared_sp_delegated, sharedSpReason),
  ];
}

function choice(
  value: AccessAs,
  isAvailable: boolean,
  reasonWhenUnavailable: string | null,
): AccessAsChoice {
  return {
    value,
    label: ACCESS_AS_LABEL[value],
    isAvailable,
    reason: isAvailable ? null : reasonWhenUnavailable,
  };
}

export function checkResultLabel(result: ResourceView["check_result"]): string {
  return result === "ok"
    ? "Verified: the attacher's token reached it"
    : "Not verified: no check exists for this type";
}

// -------------------------------------------------------------- attach form

/** What the inputs hold: text, as typed (`accessAs` is `""` until chosen). */
export interface AttachDraft {
  key: string;
  type: string;
  target: string;
  permission: string;
  accessAs: AccessAs | "";
  /** The person accepted an offer of the release's `databricks.yml`. */
  fromBundle: boolean;
}

export type AttachField = keyof AttachDraft;

const DEFAULT_TYPE = RESOURCE_TYPES[0]?.type ?? "sql_warehouse";

function firstLevel(type: string): string {
  return resourceTypeInfo(type)?.levels[0] ?? "";
}

export function emptyAttachDraft(): AttachDraft {
  return {
    key: "",
    type: DEFAULT_TYPE,
    target: "",
    permission: firstLevel(DEFAULT_TYPE),
    accessAs: "",
    fromBundle: false,
  };
}

/**
 * An offer of the bundle file, filled in. A level that is not one of the type
 * (`IS_OWNER`) is left empty so the person picks one; a target the file left
 * unresolved (`${var.endpoint}`) arrives as `null` and is typed.
 */
export function attachDraftFromOffer(offer: BundleResource): AttachDraft {
  const info = resourceTypeInfo(offer.type);
  return {
    key: offer.key,
    type: offer.type,
    target: offer.target ?? "",
    permission: info?.levels.includes(offer.permission) ? offer.permission : "",
    accessAs: "",
    fromBundle: true,
  };
}

/** A requirement that is still missing: its offer, or else its key and the type its name suggests. */
export function attachDraftFromRequirement(
  requirement: Requirement,
): AttachDraft {
  if (requirement.offer !== null)
    return attachDraftFromOffer(requirement.offer);
  const type =
    requirement.suggested_type !== null &&
    resourceTypeInfo(requirement.suggested_type) !== null
      ? requirement.suggested_type
      : DEFAULT_TYPE;
  return {
    ...emptyAttachDraft(),
    key: requirement.key,
    type,
    permission: firstLevel(type),
  };
}

/** A new type keeps the level when it fits, takes its first otherwise, and a secret has no identity. */
export function changeDraftType(draft: AttachDraft, type: string): AttachDraft {
  const info = resourceTypeInfo(type);
  return {
    ...draft,
    type,
    permission: info?.levels.includes(draft.permission)
      ? draft.permission
      : firstLevel(type),
    accessAs: info?.hasAccessAs === false ? "" : draft.accessAs,
  };
}

export interface AttachSubmission {
  /** The path parameter. */
  key: string;
  request: AttachResourceRequest;
}

const SCHEMA_FIELD_TO_DRAFT_FIELD: Record<string, AttachField> = {
  type: "type",
  target: "target",
  permission: "permission",
  access_as: "accessAs",
};

const SECRET_TARGET_HELP =
  "Use scope/key, such as sales-secrets/openai-api-key. Never type the secret value.";

export function buildAttachRequest(
  app: AppDetail,
  draft: AttachDraft,
): FormResult<AttachSubmission> {
  const key = draft.key.trim();
  const target = draft.target.trim();
  const info = resourceTypeInfo(draft.type);
  const errors: FormErrors = {};

  if (key === "") {
    errors.key = "Enter a key; the app's env entries refer to it.";
  } else if (!ResourceKey.safeParse(key).success) {
    errors.key =
      "Use lower-case letters, digits, '.', '_' and '-', at most 63 characters.";
  } else if (app.resources.some((resource) => resource.key === key)) {
    errors.key =
      "A resource with this key is already attached. Detach it first to attach it again.";
  }

  if (info === null) errors.type = "Pick a type from the list.";
  if (target === "") {
    errors.target = `Enter the ${(info?.targetLabel ?? "target").toLowerCase()}.`;
  }
  if (info !== null && !info.levels.includes(draft.permission)) {
    errors.permission = `Use one of ${info.levels.join(", ")}.`;
  }

  const accessAs = info?.hasAccessAs === false ? null : draft.accessAs;
  if (info?.hasAccessAs === true) {
    if (draft.accessAs === "") {
      errors.accessAs = "Choose the identity the app reaches it as.";
    } else {
      const chosen = accessAsChoices(app).find(
        (candidate) => candidate.value === draft.accessAs,
      );
      if (chosen !== undefined && !chosen.isAvailable) {
        errors.accessAs = chosen.reason ?? "Not available for this app.";
      }
    }
  }

  const request = {
    type: draft.type,
    target,
    permission: draft.permission,
    ...(accessAs === null || accessAs === "" ? {} : { access_as: accessAs }),
    ...(draft.fromBundle ? { from_bundle: true } : {}),
  };
  const parsed = AttachResourceRequest.safeParse(request);
  const schemaErrors: FormErrors = {};
  if (!parsed.success) {
    for (const [field, message] of Object.entries(
      errorsFromZod(parsed.error),
    )) {
      schemaErrors[SCHEMA_FIELD_TO_DRAFT_FIELD[field] ?? FORM_ERROR_KEY] =
        message;
    }
    if (draft.type === "secret" && schemaErrors.target !== undefined) {
      schemaErrors.target = SECRET_TARGET_HELP;
    }
  }

  const allErrors = { ...schemaErrors, ...errors };
  if (parsed.success && Object.keys(allErrors).length === 0) {
    return { ok: true, body: { key, request: parsed.data } };
  }
  return formFailure(allErrors);
}

// ------------------------------------------------------------- requirements

export interface RequirementRow {
  key: string;
  state: Requirement["state"];
  offer: BundleResource | null;
  suggestedType: string | null;
  /** The attached resource that fills it, if any. */
  resource: ResourceView | null;
}

export function requirementRows(
  resources: readonly ResourceView[],
  requirements: RequirementsResponse,
): RequirementRow[] {
  return requirements.items.map((requirement) => ({
    key: requirement.key,
    state: requirement.state,
    offer: requirement.offer,
    suggestedType: requirement.suggested_type,
    resource:
      resources.find((resource) => resource.key === requirement.key) ?? null,
  }));
}

export interface RequirementsSummary {
  attached: number;
  missing: number;
  text: string;
}

export function requirementsSummary(
  requirements: RequirementsResponse,
): RequirementsSummary {
  const attached = requirements.items.filter(
    (item) => item.state === "attached",
  ).length;
  const missing = requirements.items.length - attached;
  const text =
    requirements.items.length === 0
      ? "Nothing is required"
      : `${attached} of ${requirements.items.length} attached`;
  return { attached, missing, text };
}

// ------------------------------------------------------------------- detach

/** What still reads a resource that is about to be detached; `null` when nothing does. */
export function detachWarning(app: AppDetail, key: string): string | null {
  const parts: string[] = [];
  const envNames = app.config.env
    .filter((entry) => entry.value_from === key)
    .map((entry) => entry.name);
  if (envNames.length > 0) {
    parts.push(
      `Still read by the env ${envNames.length === 1 ? "entry" : "entries"} ${envNames.join(", ")}.`,
    );
  }
  if (
    app.config.own_sp_client_id_key === key ||
    app.config.own_sp_client_secret_key === key
  ) {
    parts.push("Holds the credentials of the app's own service principal.");
  }
  return parts.length === 0 ? null : parts.join(" ");
}
