/** The app resource: register, edit, owner, state, and the AppDetail page answer. */
import { z } from "./zod.ts";
import { RequirementsResponse, ResourceView } from "./config.ts";
import { DelegationStatus } from "./delegations.ts";
import { EgressRule } from "./egress.ts";
import { AppState, DesiredState, Runtime } from "./enums.ts";
import { AppGrant } from "./grants.ts";
import {
  LowerCaseEmail,
  MemoryBudgetMb,
  NotBlank,
  Slug,
  Time,
  WorkspacePath,
} from "./primitives.ts";
import { ReleaseSummary } from "./releases.ts";
import { EnvEntry } from "./shared.ts";

export const RegisterAppRequest = z.strictObject({
  slug: Slug,
  display_name: NotBlank,
  source_path: WorkspacePath,
  // Left out (or null), the app gets the platform's default budget.
  memory_budget_mb: MemoryBudgetMb.nullish(),
  owner: LowerCaseEmail,
});

export type RegisterAppRequest = z.infer<typeof RegisterAppRequest>;

export const UpdateAppRequest = z
  .strictObject({
    version: z.int(),
    slug: Slug.nullish(),
    display_name: NotBlank.nullish(),
    source_path: WorkspacePath.nullish(),
    memory_budget_mb: MemoryBudgetMb.nullish(),
  })
  .refine(
    ({ slug, display_name, source_path, memory_budget_mb }) =>
      [slug, display_name, source_path, memory_budget_mb].some(
        (field) => field != null,
      ),
    { message: "at least one field besides version must be present" },
  );

export type UpdateAppRequest = z.infer<typeof UpdateAppRequest>;

export const SetOwnerRequest = z.strictObject({
  version: z.int(),
  owner: LowerCaseEmail,
});

export type SetOwnerRequest = z.infer<typeof SetOwnerRequest>;

export const AppStateResponse = z.object({
  state: AppState,
  desired_state: DesiredState,
});

export type AppStateResponse = z.infer<typeof AppStateResponse>;

export const AppDetail = z.object({
  slug: z.string(),
  display_name: z.string(),
  owner: z.string(),
  source_path: z.string(),
  memory_budget_mb: z.int(),
  state: AppState,
  runtime: Runtime.nullable(),
  restart_to_apply: z.boolean(),
  version: z.int(),
  created_by: z.string(),
  created_at: Time,
  updated_by: z.string(),
  updated_at: Time,
  releases: z.array(ReleaseSummary),
  requirements: RequirementsResponse,
  resources: z.array(ResourceView),
  config: z.object({
    desired_state: DesiredState,
    target_release_no: z.int().nullable(),
    env: z.array(EnvEntry),
    wheel_sources: z.array(z.string()),
    health_path: z.string(),
    health_deadline_s: z.int(),
    own_sp_client_id_key: z.string().nullable(),
    own_sp_client_secret_key: z.string().nullable(),
    version: z.int(),
    updated_by: z.string(),
    updated_at: Time,
  }),
  delegation: DelegationStatus,
  egress: z.array(EgressRule),
  grants: z.array(AppGrant),
});

export type AppDetail = z.infer<typeof AppDetail>;
