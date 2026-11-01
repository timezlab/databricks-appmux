/** App configuration, attached resources and the requirements a release declares. */
import { z } from "./zod.ts";
import { AccessAs } from "./enums.ts";
import { ResourceKey, Time } from "./primitives.ts";
import { EnvEntryRequest } from "./shared.ts";

const HEALTH_DEADLINE_MIN_S = 5;

const HEALTH_DEADLINE_MAX_S = 600;

/** `scope/key` of a secret resource: one slash, both parts present. */
const SECRET_REFERENCE_PATTERN = /^[^/]+\/[^/]+$/;

export const UpdateConfigRequest = z
  .strictObject({
    version: z.int(),
    env: z.array(EnvEntryRequest),
    wheel_sources: z.array(z.string()),
    health_path: z.string().startsWith("/"),
    health_deadline_s: z
      .int()
      .min(HEALTH_DEADLINE_MIN_S)
      .max(HEALTH_DEADLINE_MAX_S),
    own_sp_client_id_key: ResourceKey.nullable(),
    own_sp_client_secret_key: ResourceKey.nullable(),
  })
  .refine(
    (config) =>
      (config.own_sp_client_id_key === null) ===
      (config.own_sp_client_secret_key === null),
    {
      message: "set both own_sp_*_key fields, or neither",
      path: ["own_sp_client_secret_key"],
    },
  );

export type UpdateConfigRequest = z.infer<typeof UpdateConfigRequest>;

const SECRET_TYPE = "secret";

const SECRET_PERMISSION = "READ";

/**
 * `access_as` is required for every type but a `secret`, which has none; a
 * secret is a `scope/key` reference with the one level `READ`. Other types and
 * levels are the code's catalogue (data model 4.7), which the server owns.
 */
export const AttachResourceRequest = z
  .strictObject({
    type: z.string(),
    target: z.string(),
    permission: z.string(),
    access_as: AccessAs.nullish(),
    from_bundle: z.boolean().nullish(),
  })
  .check((context) => {
    const request = context.value;
    if (request.type !== SECRET_TYPE) {
      if (request.access_as == null) {
        context.issues.push({
          code: "custom",
          message: "access_as is required for every type but secret",
          path: ["access_as"],
          input: request.access_as,
        });
      }
      return;
    }
    if (request.access_as != null) {
      context.issues.push({
        code: "custom",
        message: "a secret has no access_as",
        path: ["access_as"],
        input: request.access_as,
      });
    }
    if (request.permission !== SECRET_PERMISSION) {
      context.issues.push({
        code: "custom",
        message: `a secret takes the level ${SECRET_PERMISSION}`,
        path: ["permission"],
        input: request.permission,
      });
    }
    if (!SECRET_REFERENCE_PATTERN.test(request.target)) {
      context.issues.push({
        code: "custom",
        message: "a secret target is a scope/key reference",
        path: ["target"],
        input: request.target,
      });
    }
  });

export type AttachResourceRequest = z.infer<typeof AttachResourceRequest>;

export const BundleResource = z.object({
  key: z.string(),
  type: z.string(),
  target: z.string().nullable(),
  permission: z.string(),
});

export type BundleResource = z.infer<typeof BundleResource>;

export const Requirement = z.object({
  key: z.string(),
  state: z.enum(["attached", "missing"]),
  offer: BundleResource.nullable(),
  suggested_type: z.string().nullable(),
});

export type Requirement = z.infer<typeof Requirement>;

export const RequirementsResponse = z.object({
  release_no: z.int().nullable(),
  items: z.array(Requirement),
  unreferenced_offers: z.array(BundleResource),
});

export type RequirementsResponse = z.infer<typeof RequirementsResponse>;

export const ResourceView = z.object({
  key: z.string(),
  type: z.string(),
  target: z.string(),
  permission: z.string(),
  access_as: AccessAs.nullable(),
  check_result: z.enum(["ok", "unverified"]),
  attached_by: z.string(),
  attached_at: Time,
});

export type ResourceView = z.infer<typeof ResourceView>;

export const ConfigChangeResult = z.object({
  config_version: z.int(),
  release_no: z.int().nullable(),
});

export type ConfigChangeResult = z.infer<typeof ConfigChangeResult>;
