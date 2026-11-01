/** Who may open and manage an app. */
import { z } from "./zod.ts";
import { AppPermission, GrantType } from "./enums.ts";
import { LowerCaseEmail, NotBlank, Time } from "./primitives.ts";

/**
 * Who a grant names. `everyone` is `*` and `CAN_USE` only (FR-022). That the
 * owner cannot be named with a lower level needs the app, so the server checks
 * it.
 */
export const GrantRequest = z.discriminatedUnion("type", [
  z.strictObject({
    type: z.literal("user"),
    name: LowerCaseEmail,
    permission: AppPermission,
  }),
  z.strictObject({
    type: z.literal("group"),
    name: NotBlank.refine(
      (name) => name !== "*",
      "'*' names everyone, not a group",
    ),
    permission: AppPermission,
  }),
  z.strictObject({
    type: z.literal("everyone"),
    name: z.literal("*"),
    permission: z.literal("CAN_USE"),
  }),
]);

export type GrantRequest = z.infer<typeof GrantRequest>;

export const AppGrant = z.object({
  type: GrantType,
  name: z.string(),
  permission: AppPermission,
  granted_by: z.string(),
  granted_at: Time,
});

export type AppGrant = z.infer<typeof AppGrant>;
