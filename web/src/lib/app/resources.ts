/**
 * The app's resources and requirements (design 8.2, data model 4.7). The
 * catalogue of resource types is data from `GET /resources/types`; the page
 * holds no copy of it, so a type the server adds shows up without a change
 * here, and one the page cannot match is shown by its raw name. The Add form
 * checks what the contract can check from the page's data and leaves the
 * attach check itself (the attacher's own token reaching the target) to the
 * server.
 */
import { AttachResourceRequest, ResourceKey } from "@/lib/api";
import type {
  AccessAs,
  AppDetail,
  BundleResource,
  Requirement,
  RequirementsResponse,
  ResourceSuggestion,
  ResourceTypeEntry,
  ResourceView,
} from "@/lib/api";
import { FORM_ERROR_KEY, errorsFromZod, formFailure } from "./forms";
import type { FormErrors, FormResult } from "./forms";

// ---------------------------------------------------------------- catalogue

export type ResourceCatalogue = readonly ResourceTypeEntry[];

export function findResourceType(
  catalogue: ResourceCatalogue | null,
  type: string,
): ResourceTypeEntry | null {
  return catalogue?.find((entry) => entry.type === type) ?? null;
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The noun of a type, capitalised ("Secret", "SQL warehouse"); the wire value
 * while the catalogue loads or for a type it does not list.
 */
export function resourceTypeLabel(
  catalogue: ResourceCatalogue | null,
  type: string,
): string {
  const entry = findResourceType(catalogue, type);
  return entry === null ? type : capitalise(entry.label);
}

/** A permission level in words: `CAN_MANAGE_RUN` is "Can manage run". */
export function resourceLevelLabel(level: string): string {
  return capitalise(level.toLowerCase().replaceAll("_", " "));
}

/** A secret scope offered by the suggestions: a prefix to go on typing from, not a target. */
export function isScopePrefix(
  type: string,
  suggestion: ResourceSuggestion,
): boolean {
  return type === "secret" && suggestion.target.endsWith("/");
}

// ------------------------------------------------------------- access modes

const ACCESS_MODES: readonly AccessAs[] = ["obo", "own_sp", "shared_sp"];

const ACCESS_AS_LABEL: Record<AccessAs, string> = {
  obo: "As the visitor (OBO)",
  own_sp: "As the app's own service principal",
  shared_sp: "As the shared service principal",
};

/** How the child reaches a resource; a secret has no identity, appmux reads it at start. */
export function accessAsLabel(accessAs: AccessAs | null): string {
  return accessAs === null
    ? "Read by appmux when the app starts"
    : ACCESS_AS_LABEL[accessAs];
}

export interface AccessModeChoice {
  value: AccessAs;
  label: string;
  isAvailable: boolean;
  /** Why it cannot be chosen; `null` when it can. */
  reason: string | null;
  /** How to make it available; `null` when it is, or when nothing can be done. */
  howToGet: string | null;
}

const ASK_FOR_DELEGATION =
  "An admin of this app can ask a platform admin for it with Request a delegation, under Authorization.";

/**
 * The three ways the app can reach a resource of this type, each with whether
 * it can be chosen now: the type must allow it (the shared service principal
 * only for a type with an attach check) and the app must have it (a
 * delegation, or its own service principal's keys).
 */
export function accessModeChoices(
  app: AppDetail,
  entry: ResourceTypeEntry,
): AccessModeChoice[] {
  const { obo_delegated, shared_sp_delegated, shared_sp_delegation_allowed } =
    app.delegation;
  const hasOwnSpKeys =
    app.config.own_sp_client_id_key !== null &&
    app.config.own_sp_client_secret_key !== null;
  const typeLabel = entry.label;

  return ACCESS_MODES.map((value) => {
    const label = ACCESS_AS_LABEL[value];
    const off = (reason: string, howToGet: string | null) => ({
      value,
      label,
      isAvailable: false,
      reason,
      howToGet,
    });
    if (!entry.access_as.includes(value)) {
      return value === "shared_sp"
        ? off(
            `The shared service principal needs a verified attach check, and a ${typeLabel} has none.`,
            null,
          )
        : off(`A ${typeLabel} cannot be reached this way.`, null);
    }
    if (value === "obo" && !obo_delegated) {
      return off("OBO is not delegated to this app.", ASK_FOR_DELEGATION);
    }
    if (value === "own_sp" && !hasOwnSpKeys) {
      return off(
        "The app has no service principal keys of its own.",
        "Attach its client id and client secret as secrets, then name the two keys in the app's settings.",
      );
    }
    if (value === "shared_sp" && !shared_sp_delegated) {
      return shared_sp_delegation_allowed
        ? off(
            "The shared service principal is not delegated to this app.",
            ASK_FOR_DELEGATION,
          )
        : off(
            "A platform admin has switched shared service principal delegation off for every app.",
            null,
          );
    }
    return { value, label, isAvailable: true, reason: null, howToGet: null };
  });
}

export function checkResultLabel(result: ResourceView["check_result"]): string {
  return result === "ok"
    ? "Verified: the attacher's token reached it"
    : "Not verified: no check exists for this type";
}

// ---------------------------------------------------------------------- key

const KEY_MAX_LENGTH = 63;

/** Text as a resource key: lower case, accents dropped, every other run of characters a `-`. */
function keySlug(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9.]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .slice(0, KEY_MAX_LENGTH)
    .replace(/[-.]+$/, "");
}

/**
 * The key a new resource is filled with: the object's name, or the type when
 * no object is chosen yet, made unique among the keys the app has (`-2`,
 * `-3`, …) and kept to the contract's form.
 */
export function deriveResourceKey(
  type: string,
  objectName: string | null,
  takenKeys: readonly string[],
): string {
  const base = keySlug(objectName ?? "") || keySlug(type) || "resource";
  if (!takenKeys.includes(base)) return base;
  for (let counter = 2; ; counter += 1) {
    const suffix = `-${counter}`;
    const stem = base
      .slice(0, KEY_MAX_LENGTH - suffix.length)
      .replace(/[-.]+$/, "");
    const candidate = `${stem}${suffix}`;
    if (!takenKeys.includes(candidate)) return candidate;
  }
}

// --------------------------------------------------------------- add form

/** What the Add panel holds, as typed (`type`, `permission` and `accessAs` are `""` until chosen). */
export interface AttachDraft {
  key: string;
  /** The person typed the key, so it no longer follows the type and the object. */
  isKeyEdited: boolean;
  type: string;
  target: string;
  /** What the key is filled from: the chosen object's name, or a typed secret's key. */
  objectName: string | null;
  permission: string;
  accessAs: AccessAs | "";
  /** The person accepted an offer of the release's `databricks.yml`. */
  fromBundle: boolean;
}

export type AttachField = Exclude<
  keyof AttachDraft,
  "isKeyEdited" | "objectName" | "fromBundle"
>;

export function emptyAttachDraft(): AttachDraft {
  return {
    key: "",
    isKeyEdited: false,
    type: "",
    target: "",
    objectName: null,
    permission: "",
    accessAs: "",
    fromBundle: false,
  };
}

function withDerivedKey(
  draft: AttachDraft,
  takenKeys: readonly string[],
): AttachDraft {
  if (draft.isKeyEdited || draft.type === "") return draft;
  return {
    ...draft,
    key: deriveResourceKey(draft.type, draft.objectName, takenKeys),
  };
}

/**
 * An offer of the bundle file, filled in. Its key is the one the release
 * refers to, so it is kept. A level that is not one of the type (`IS_OWNER`)
 * is left empty so the person picks one; a target the file left unresolved
 * (`${var.endpoint}`) arrives as `null` and is chosen or typed.
 */
export function attachDraftFromOffer(
  offer: BundleResource,
  catalogue: ResourceCatalogue | null,
): AttachDraft {
  const entry = findResourceType(catalogue, offer.type);
  return {
    ...emptyAttachDraft(),
    key: offer.key,
    isKeyEdited: true,
    type: offer.type,
    target: offer.target ?? "",
    permission: entry?.permissions.includes(offer.permission)
      ? offer.permission
      : "",
    fromBundle: true,
  };
}

/** A requirement that is still missing: its offer, or else its key and the type its name suggests. */
export function attachDraftFromRequirement(
  requirement: Requirement,
  catalogue: ResourceCatalogue | null,
): AttachDraft {
  if (requirement.offer !== null) {
    return attachDraftFromOffer(requirement.offer, catalogue);
  }
  const entry =
    requirement.suggested_type === null
      ? null
      : findResourceType(catalogue, requirement.suggested_type);
  return {
    ...emptyAttachDraft(),
    key: requirement.key,
    isKeyEdited: true,
    type: entry?.type ?? "",
    permission: entry?.permissions[0] ?? "",
  };
}

/** A new type starts the object again, takes its first level, and keeps an access mode it allows. */
export function chooseDraftType(
  draft: AttachDraft,
  entry: ResourceTypeEntry,
  takenKeys: readonly string[],
): AttachDraft {
  return withDerivedKey(
    {
      ...draft,
      type: entry.type,
      target: "",
      objectName: null,
      permission: entry.permissions[0] ?? "",
      accessAs:
        draft.accessAs !== "" && entry.access_as.includes(draft.accessAs)
          ? draft.accessAs
          : "",
      fromBundle: false,
    },
    takenKeys,
  );
}

/** An object picked from the suggestions: its target, and its name for the key. */
export function chooseDraftObject(
  draft: AttachDraft,
  suggestion: ResourceSuggestion,
  takenKeys: readonly string[],
): AttachDraft {
  return withDerivedKey(
    {
      ...draft,
      target: suggestion.target,
      objectName: suggestion.label,
      permission: suggestion.permissions.includes(draft.permission)
        ? draft.permission
        : (suggestion.permissions[0] ?? draft.permission),
    },
    takenKeys,
  );
}

/** The key part of a typed `scope/key`, or `null`. */
function typedSecretKey(target: string): string | null {
  const slash = target.indexOf("/");
  const key = slash === -1 ? "" : target.slice(slash + 1).trim();
  return key === "" ? null : key;
}

/** A target typed by hand: a secret's key still names the resource; any other text does not. */
export function typeDraftTarget(
  draft: AttachDraft,
  target: string,
  takenKeys: readonly string[],
): AttachDraft {
  return withDerivedKey(
    {
      ...draft,
      target,
      objectName: draft.type === "secret" ? typedSecretKey(target) : null,
    },
    takenKeys,
  );
}

export function editDraftKey(draft: AttachDraft, key: string): AttachDraft {
  return { ...draft, key, isKeyEdited: true };
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
  entry: ResourceTypeEntry | null,
): FormResult<AttachSubmission> {
  const key = draft.key.trim();
  const target = draft.target.trim();
  const errors: FormErrors = {};

  if (entry === null) {
    errors.type =
      draft.type === ""
        ? "Choose a type."
        : `appmux does not offer the type ${draft.type}. Choose one from the list.`;
  } else {
    if (target === "") {
      errors.target = `Choose one from the list or type it, such as ${entry.target_hint}.`;
    }
    if (!entry.permissions.includes(draft.permission)) {
      errors.permission =
        draft.permission === ""
          ? "Choose a permission."
          : `Use one of ${entry.permissions.map(resourceLevelLabel).join(", ")}.`;
    }
    if (entry.access_as.length > 0) {
      const chosen = accessModeChoices(app, entry).find(
        (choice) => choice.value === draft.accessAs,
      );
      if (chosen === undefined) {
        errors.accessAs = "Choose how the app reaches it.";
      } else if (!chosen.isAvailable) {
        errors.accessAs = chosen.reason ?? "Not available for this app.";
      }
    }
  }

  if (key === "") {
    errors.key = "Enter a key; the app's env entries refer to it.";
  } else if (!ResourceKey.safeParse(key).success) {
    errors.key =
      "Use lower-case letters, digits, '.', '_' and '-', starting with a letter or digit, at most 63 characters.";
  } else if (app.resources.some((resource) => resource.key === key)) {
    errors.key =
      "A resource with this key is already attached. Detach it first to attach it again.";
  }

  const hasAccessAs = entry !== null && entry.access_as.length > 0;
  const request = {
    type: draft.type,
    target,
    permission: draft.permission,
    ...(hasAccessAs && draft.accessAs !== ""
      ? { access_as: draft.accessAs }
      : {}),
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

/**
 * Said beside an attached resource that nothing reads: the active release
 * does not refer to it and it holds no own service principal key. `null`
 * while no release has been read, or when something reads it.
 */
export function unusedResourceNote(app: AppDetail, key: string): string | null {
  const { requirements, config } = app;
  if (requirements.release_no === null) return null;
  const isRequired = requirements.items.some((item) => item.key === key);
  const isOwnSpKey =
    config.own_sp_client_id_key === key ||
    config.own_sp_client_secret_key === key;
  return isRequired || isOwnSpKey
    ? null
    : `Release ${requirements.release_no} does not refer to it.`;
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
