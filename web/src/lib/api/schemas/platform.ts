/** The platform page: settings, admins, self-check, audit log. */
import { z } from "./zod.ts";
import { EgressRule } from "./egress.ts";
import { PackageMode } from "./enums.ts";
import { LowerCaseEmail, NotBlank, Slug, Time } from "./primitives.ts";
import { Principal } from "./shared.ts";

export const PutSettingRequest = z.strictObject({ value: z.json() });

export type PutSettingRequest = z.infer<typeof PutSettingRequest>;

/** A platform admin entry a client sends: a user or a group, never `everyone`. */
const AdminEntryRequest = z.discriminatedUnion("type", [
  z.strictObject({ type: z.literal("user"), name: LowerCaseEmail }),
  z.strictObject({ type: z.literal("group"), name: NotBlank }),
]);

/** The server compares type and lower-cased name, so `Data-Platform` and `data-platform` are one group. */
function hasNoDuplicateAdmin(
  admins: readonly { type: string; name: string }[],
): boolean {
  const seen = new Set<string>();
  for (const admin of admins) {
    const key = JSON.stringify([admin.type, admin.name.toLowerCase()]);
    if (seen.has(key)) return false;
    seen.add(key);
  }
  return true;
}

export const PutAdminsRequest = z.strictObject({
  admins: z
    .array(AdminEntryRequest)
    .refine(hasNoDuplicateAdmin, "an admin is listed twice")
    .refine(
      (admins) => admins.some((admin) => admin.type === "user"),
      "at least one user must remain",
    ),
  reason: NotBlank,
});

export type PutAdminsRequest = z.infer<typeof PutAdminsRequest>;

export const SelfCheckResult = z.object({
  passed: z.boolean(),
  ran_at: Time,
  duration_ms: z.int(),
  checks: z.array(
    z.object({
      name: z.string(),
      passed: z.boolean(),
      detail: z.string().nullable(),
    }),
  ),
});

export type SelfCheckResult = z.infer<typeof SelfCheckResult>;

export const PlatformApp = z.object({
  slug: z.string(),
  display_name: z.string(),
  source_path: z.string(),
  memory_budget_mb: z.int(),
  owner: z.string(),
  obo_delegated: z.boolean(),
  shared_sp_delegated: z.boolean(),
  egress: z.array(EgressRule),
  /** The `apps` version a `PATCH /apps/{app}` sends. */
  version: z.int(),
});

export type PlatformApp = z.infer<typeof PlatformApp>;

export const PlatformView = z.object({
  settings: z.object({
    memory_reserve_mb: z.int(),
    package_mode: PackageMode,
    default_wheel_sources: z.array(z.string()),
    shared_sp_delegation_allowed: z.boolean(),
    max_releases_per_app: z.int(),
    max_locked_releases: z.int(),
    request_timeout_s: z.int(),
    max_request_body_mb: z.int(),
    max_concurrent_requests: z.int(),
    build_timeout_s: z.int(),
    max_processes_per_child: z.int(),
    child_cpu_pin: z.boolean(),
    attach_check_warehouse_id: z.string().nullable(),
  }),
  admins: z.array(Principal),
  egress_global: z.array(EgressRule),
  self_check: SelfCheckResult,
  memory: z.object({
    limit_mb: z.int(),
    reserve_mb: z.int(),
    committed_mb: z.int(),
    free_mb: z.int(),
  }),
  apps: z.array(PlatformApp),
});

export type PlatformView = z.infer<typeof PlatformView>;

export const PlatformSetting = z.object({
  key: z.string(),
  value: z.json(),
  updated_by: z.string(),
  updated_at: Time,
});

export type PlatformSetting = z.infer<typeof PlatformSetting>;

export const AdminList = z.object({ items: z.array(Principal) });

export type AdminList = z.infer<typeof AdminList>;

export const AuditRow = z.object({
  id: z.int(),
  ts: Time,
  actor: z.string(),
  action: z.string(),
  app_slug: z.string().nullable(),
  detail: z.record(z.string(), z.unknown()),
  reason: z.string().nullable(),
});

export type AuditRow = z.infer<typeof AuditRow>;

export const AuditList = z.object({
  from: Time,
  to: Time,
  items: z.array(AuditRow),
});

export type AuditList = z.infer<typeof AuditList>;

// admin-api.md, AuditList: "`limit` defaults to 100 and is at most 1000".
const AUDIT_LIMIT_MAX = 1000;

/**
 * The query string of `GET /audit`. It is no body of the contract, so it has no
 * example file; `listAudit` checks it before it builds the URL. The server
 * defaults a missing `to` to now, `from` to 24 hours before `to`, `limit` to 100.
 */
export const AuditQuery = z.strictObject({
  /** Filters by slug. */
  app: Slug.optional(),
  from: Time.optional(),
  to: Time.optional(),
  limit: z.int().min(1).max(AUDIT_LIMIT_MAX).optional(),
});

export type AuditQuery = z.infer<typeof AuditQuery>;
