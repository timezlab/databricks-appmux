/** Delegation (OBO, shared SP) of an app: the status and the reason a grant or revoke carries. */
import { z } from "./zod.ts";
import { Reason } from "./primitives.ts";

/** The body of a delegation `PUT`, and the query parameters of its `DELETE`. */
export const DelegationReasonRequest = z.strictObject({
  version: z.int(),
  reason: Reason,
});

export type DelegationReasonRequest = z.infer<typeof DelegationReasonRequest>;

export const DelegationStatus = z.object({
  obo_delegated: z.boolean(),
  shared_sp_delegated: z.boolean(),
  shared_sp_delegation_allowed: z.boolean(),
});

export type DelegationStatus = z.infer<typeof DelegationStatus>;
