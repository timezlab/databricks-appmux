/** Egress rules: the editor's request schemas and the stored rule in a response. */
import { z } from "./zod.ts";
import {
  APPMUX_PORT_PLACEHOLDER,
  CONTAINER_IP_CIDR_PATTERN,
  CONTAINER_IP_PLACEHOLDER,
  LINK_LOCAL_RANGE,
  MAX_PORT,
  holdsProbeAddress,
  isValidEgressPort,
  overlapsLinkLocal,
  parseIpv4Cidr,
} from "./egress-cidr.ts";

const EGRESS_ACTIONS = ["allow", "deny"] as const;

/** An egress rule in a response: the server's stored value, shown as it is. */
export const EgressRule = z.object({
  action: z.enum(EGRESS_ACTIONS),
  cidr: z.string(),
  port: z.string(),
  comment: z.string(),
});

export type EgressRule = z.infer<typeof EgressRule>;

/**
 * The destination of an egress rule: an IPv4 CIDR with a prefix length and no
 * host bits set (v1 has no IPv6), or `@container_ip` with an optional prefix
 * length. The server refuses everything else, so the editor says so too.
 */
const EgressCidr = z.string().superRefine((cidr, context) => {
  if (CONTAINER_IP_CIDR_PATTERN.test(cidr)) return;
  const range = parseIpv4Cidr(cidr);
  if (range.kind === "ok") return;
  context.addIssue({
    code: "custom",
    message:
      range.kind === "host-bits"
        ? "must have no host bits set (write the network address, such as 10.20.0.0/16)"
        : "must be an IPv4 CIDR such as 10.20.0.0/16, or start with @container_ip (@container_ip/32)",
  });
});

/**
 * An egress rule a client sends. The editor refuses an `allow` that overlaps
 * `@container_ip`, `@appmux_port` or `169.254.0.0/16`, because `self_check()`
 * assumes those stay closed (design 6.3). The address `@container_ip` resolves
 * to is unknown here, so the check is the server's schema check: the
 * placeholders, the link-local range, and any `allow` whose range holds
 * 0.0.0.0 (the address the server's schema probes with). A plain CIDR that
 * happens to cover the real container address is left to the route.
 */
export const EgressRuleRequest = z
  .strictObject({
    action: z.enum(EGRESS_ACTIONS),
    cidr: EgressCidr,
    port: z
      .string()
      .refine(
        isValidEgressPort,
        `any, a port from 1 to ${MAX_PORT}, or ${APPMUX_PORT_PLACEHOLDER}`,
      ),
    comment: z.string(),
  })
  .refine(
    (rule) =>
      rule.action !== "allow" ||
      !rule.cidr.startsWith(CONTAINER_IP_PLACEHOLDER),
    { message: "an allow cannot overlap @container_ip", path: ["cidr"] },
  )
  .refine(
    (rule) => rule.action !== "allow" || rule.port !== APPMUX_PORT_PLACEHOLDER,
    { message: "an allow cannot overlap @appmux_port", path: ["port"] },
  )
  .refine((rule) => rule.action !== "allow" || !overlapsLinkLocal(rule.cidr), {
    message: `an allow cannot overlap ${LINK_LOCAL_RANGE}`,
    path: ["cidr"],
  })
  .refine((rule) => rule.action !== "allow" || !holdsProbeAddress(rule.cidr), {
    message: "an allow cannot overlap @container_ip (its range holds 0.0.0.0)",
    path: ["cidr"],
  });

export type EgressRuleRequest = z.infer<typeof EgressRuleRequest>;

export const PutEgressRequest = z.strictObject({
  rules: z.array(EgressRuleRequest),
});

export type PutEgressRequest = z.infer<typeof PutEgressRequest>;

export const PutAppEgressRequest = z.strictObject({
  version: z.int(),
  rules: z.array(EgressRuleRequest),
});

export type PutAppEgressRequest = z.infer<typeof PutAppEgressRequest>;

export const EgressRules = z.object({ items: z.array(EgressRule) });

export type EgressRules = z.infer<typeof EgressRules>;
