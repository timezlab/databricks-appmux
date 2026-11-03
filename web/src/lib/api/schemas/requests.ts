/** Requests to a platform admin (register, delegation, settings) and their decisions. */
import { z } from "./zod.ts";
import { RequestKind, RequestStatus } from "./enums.ts";
import { PLATFORM_FIELDS, Reason, Slug, Time } from "./primitives.ts";

// No memory budget: a new app starts with the platform's default (`default_memory_budget_mb`).
const RegisterPayload = z.strictObject({
  slug: PLATFORM_FIELDS.slug,
  display_name: PLATFORM_FIELDS.display_name,
  source_path: PLATFORM_FIELDS.source_path,
});

const DelegationPayload = z
  .strictObject({
    obo: z.literal(true).nullish(),
    shared_sp: z.literal(true).nullish(),
  })
  .refine(
    ({ obo, shared_sp }) => obo != null || shared_sp != null,
    "ask for obo, shared_sp or both",
  );

const SettingsPayload = z
  .strictObject({
    slug: PLATFORM_FIELDS.slug.nullish(),
    display_name: PLATFORM_FIELDS.display_name.nullish(),
    source_path: PLATFORM_FIELDS.source_path.nullish(),
    memory_budget_mb: PLATFORM_FIELDS.memory_budget_mb.nullish(),
  })
  .refine(
    (payload) => Object.values(payload).some((field) => field != null),
    "ask for at least one field",
  );

export const CreatePermissionRequest = z.discriminatedUnion("kind", [
  z
    .strictObject({
      kind: z.literal("register"),
      app: Slug,
      payload: RegisterPayload,
      reason: Reason,
    })
    .refine((request) => request.app === request.payload.slug, {
      message: "app must equal payload.slug",
      path: ["app"],
    }),
  z.strictObject({
    kind: z.literal("delegation"),
    app: Slug,
    payload: DelegationPayload,
    reason: Reason,
  }),
  z.strictObject({
    kind: z.literal("settings"),
    app: Slug,
    payload: SettingsPayload,
    reason: Reason,
  }),
]);

export type CreatePermissionRequest = z.infer<typeof CreatePermissionRequest>;

export const DecidePermissionRequest = z.strictObject({
  reason: z.string().nullish(),
  note: z.string().nullish(),
});

export type DecidePermissionRequest = z.infer<typeof DecidePermissionRequest>;

export const RequestRow = z.object({
  request_id: z.int(),
  app_slug: z.string().nullable(),
  kind: RequestKind,
  payload: z.record(z.string(), z.unknown()),
  reason: z.string(),
  requested_by: z.string(),
  requested_at: Time,
  status: RequestStatus,
  closed_by: z.string().nullable(),
  closed_at: Time.nullable(),
  closed_note: z.string().nullable(),
  current: z.record(z.string(), z.unknown()).nullable(),
  requires_reason: z.boolean(),
  /** Only the answer to an approval: the `apps` version after it; null otherwise. */
  app_version: z.int().nullable(),
});

export type RequestRow = z.infer<typeof RequestRow>;

export const RequestList = z.object({ items: z.array(RequestRow) });

export type RequestList = z.infer<typeof RequestList>;
