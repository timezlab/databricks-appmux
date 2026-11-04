/** The contract's closed lists (admin-api.md, "One of ..."). */
import { z } from "./zod.ts";

// ------------------------------------------------------------- closed lists

export const AppState = z.enum([
  "stopped",
  "starting",
  "running",
  "crashed",
  "no_release",
]);

export type AppState = z.infer<typeof AppState>;

export const DesiredState = z.enum(["running", "stopped"]);

export type DesiredState = z.infer<typeof DesiredState>;

export const AppPermission = z.enum(["CAN_USE", "CAN_MANAGE"]);

export type AppPermission = z.infer<typeof AppPermission>;

export const OpenMode = z.enum(["as_user", "without_token"]);

export type OpenMode = z.infer<typeof OpenMode>;

export const Runtime = z.enum(["python", "node"]);

export type Runtime = z.infer<typeof Runtime>;

export const ReleaseStatus = z.enum([
  "pending",
  "building",
  "starting",
  "healthy",
  "active",
  "failed",
  "superseded",
]);

export type ReleaseStatus = z.infer<typeof ReleaseStatus>;

export const AccessAs = z.enum(["obo", "own_sp", "shared_sp"]);

export type AccessAs = z.infer<typeof AccessAs>;

export const GrantType = z.enum([
  "user",
  "group",
  "everyone",
  "service_principal",
]);

export type GrantType = z.infer<typeof GrantType>;

export const DelegationKind = z.enum(["obo", "shared_sp"]);

export type DelegationKind = z.infer<typeof DelegationKind>;

export const RequestKind = z.enum(["register", "delegation", "settings"]);

export type RequestKind = z.infer<typeof RequestKind>;

export const RequestStatus = z.enum([
  "pending",
  "approved",
  "denied",
  "withdrawn",
]);

export type RequestStatus = z.infer<typeof RequestStatus>;

export const PackageMode = z.enum(["index", "wheelhouse", "wheelhouse+index"]);

export type PackageMode = z.infer<typeof PackageMode>;

/** The keys `PUT /platform/settings/{key}` accepts: every `platform_settings` key except `platform_admins` and `egress_global`, which have their own routes. */
export const SettingKey = z.enum([
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
]);

export type SettingKey = z.infer<typeof SettingKey>;
