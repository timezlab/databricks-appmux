/** Releases and their source listing. */
import { z } from "./zod.ts";
import { BundleResource } from "./config.ts";
import { ReleaseStatus, Runtime } from "./enums.ts";
import { Time } from "./primitives.ts";
import { EnvEntry } from "./shared.ts";

const RELEASE_NAME_MAX_LENGTH = 80;

const RELEASE_TAG_MAX_LENGTH = 32;

const RELEASE_TAGS_MAX = 10;

export const UpdateReleaseRequest = z.strictObject({
  name: z.string().min(1).max(RELEASE_NAME_MAX_LENGTH).nullable(),
  tags: z
    .array(
      z
        .string()
        .max(RELEASE_TAG_MAX_LENGTH)
        .refine((tag) => tag === tag.toLowerCase(), "must be lower case"),
    )
    .max(RELEASE_TAGS_MAX),
});

export type UpdateReleaseRequest = z.infer<typeof UpdateReleaseRequest>;

export const ReleaseSummary = z.object({
  release_no: z.int(),
  status: ReleaseStatus,
  status_reason: z.string().nullable(),
  status_at: Time,
  name: z.string().nullable(),
  tags: z.array(z.string()),
  is_target: z.boolean(),
  locked_by: z.string().nullable(),
  locked_at: Time.nullable(),
  created_by: z.string(),
  created_at: Time,
});

export type ReleaseSummary = z.infer<typeof ReleaseSummary>;

export const ReleaseDetail = ReleaseSummary.extend({
  snapshot_digest: z.string(),
  build_log_tail: z.string().nullable(),
  detected: z.object({
    runtime: Runtime,
    framework: z.string().nullable(),
    command: z.array(z.string()),
    env: z.array(EnvEntry),
    bundle_resources: z.array(BundleResource),
    warnings: z.array(z.string()),
  }),
  config: z.object({
    env: z.array(EnvEntry),
    wheel_sources: z.array(z.string()),
  }),
  wheels: z.array(
    z.object({
      sha256: z.string(),
      filename: z.string(),
      source: z.string(),
    }),
  ),
});

export type ReleaseDetail = z.infer<typeof ReleaseDetail>;

export const ReleaseCreated = z.object({
  release_no: z.int(),
  status: z.literal("pending"),
});

export type ReleaseCreated = z.infer<typeof ReleaseCreated>;

/** `200` from `POST /apps/{app}/releases`: the folder equals a live release, so none was created. */
export const ReleaseUnchanged = z.object({
  status: z.literal("up_to_date"),
  release_no: z.int(),
});

export type ReleaseUnchanged = z.infer<typeof ReleaseUnchanged>;

/** What `POST /apps/{app}/releases` answers: `202` ReleaseCreated or `200` ReleaseUnchanged. */
export const ReleaseOutcome = z.discriminatedUnion("status", [
  ReleaseCreated,
  ReleaseUnchanged,
]);

export type ReleaseOutcome = z.infer<typeof ReleaseOutcome>;

export const ReleaseSourceListing = z.object({
  release_no: z.int(),
  items: z.array(
    z.object({
      path: z.string(),
      size_bytes: z.int(),
      kind: z.enum(["text", "binary"]),
    }),
  ),
});

export type ReleaseSourceListing = z.infer<typeof ReleaseSourceListing>;

export const ReleaseSourceFile = z.object({
  release_no: z.int(),
  path: z.string(),
  size_bytes: z.int(),
  content: z.string(),
});

export type ReleaseSourceFile = z.infer<typeof ReleaseSourceFile>;
