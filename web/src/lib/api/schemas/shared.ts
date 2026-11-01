/** Shapes that more than one family embeds: Principal, EnvEntry, ErrorBody. */
import { z } from "./zod.ts";
import { ResourceKey } from "./primitives.ts";

// ------------------------------------------------------------- shared types

/** A principal in a response (`PlatformView.admins`, `AdminList`). */
export const Principal = z.object({
  type: z.enum(["user", "group", "everyone"]),
  name: z.string(),
});

export type Principal = z.infer<typeof Principal>;

function hasExactlyOneValue(entry: {
  value?: string | null | undefined;
  value_from?: string | null | undefined;
}): boolean {
  return (entry.value != null) !== (entry.value_from != null);
}

const EXACTLY_ONE_VALUE = {
  message: "exactly one of value and value_from must be set",
};

/** An env entry in a response. */
export const EnvEntry = z
  .object({
    name: z.string(),
    value: z.string().nullish(),
    value_from: z.string().nullish(),
  })
  .refine(hasExactlyOneValue, EXACTLY_ONE_VALUE);

export type EnvEntry = z.infer<typeof EnvEntry>;

/** An env entry a client sends; `value_from` names a resource of the app. */
export const EnvEntryRequest = z
  .strictObject({
    name: z.string(),
    value: z.string().nullish(),
    value_from: ResourceKey.nullish(),
  })
  .refine(hasExactlyOneValue, EXACTLY_ONE_VALUE);

export type EnvEntryRequest = z.infer<typeof EnvEntryRequest>;

// ---------------------------------------------------------- response bodies

export const ErrorBody = z.object({
  error: z.object({ code: z.string(), message: z.string() }),
});

export type ErrorBody = z.infer<typeof ErrorBody>;
