/**
 * The app's Security section in words: what its own egress rules amount to,
 * and what the addresses and ports the default rules name stand for. The rules
 * and their order are design 6.3: the app's own rules, then the global rules,
 * then default allow, with 10.0.2.0/24 always allowed.
 */
import type { EgressRule } from "@/lib/api/schemas";
import {
  ANY_PORT,
  APPMUX_PORT_PLACEHOLDER,
  CONTAINER_IP_PLACEHOLDER,
  LINK_LOCAL_RANGE,
} from "@/lib/api/schemas/egress-cidr";
import { isDenyAll } from "@/lib/platform/egress";

/** The app's virtual network: the far end of its cable and its DNS at 10.0.2.3, allowed whatever the rules say. */
export const ALWAYS_ALLOWED_RANGE = "10.0.2.0/24";

/**
 * The global rules as appmux ships them (data model 4.1, `egress_global`). A
 * person who may not read the platform's settings sees these, marked as the
 * shipped default, since a platform admin may have changed the list.
 */
export const SHIPPED_GLOBAL_EGRESS: readonly EgressRule[] = [
  {
    action: "deny",
    cidr: `${CONTAINER_IP_PLACEHOLDER}/32`,
    port: ANY_PORT,
    comment: "the container's own address",
  },
  {
    action: "deny",
    cidr: LINK_LOCAL_RANGE,
    port: ANY_PORT,
    comment: "metadata and link-local",
  },
];

/**
 * What an app's own list amounts to. New apps start pass-through (no rules);
 * an allow-list is allow rules closed by `deny 0.0.0.0/0` on any port, after
 * which the global rules are never reached.
 */
export type AppEgressMode =
  | { kind: "pass-through" }
  | { kind: "allow-list"; allowed: number }
  | { kind: "closed" }
  | { kind: "rules"; count: number };

export function appEgressMode(rules: readonly EgressRule[]): AppEgressMode {
  if (rules.length === 0) return { kind: "pass-through" };
  const last = rules.at(-1);
  if (last !== undefined && isDenyAll(last)) {
    const allowed = rules.filter((rule) => rule.action === "allow").length;
    return allowed === 0 ? { kind: "closed" } : { kind: "allow-list", allowed };
  }
  return { kind: "rules", count: rules.length };
}

/** What a rule's address stands for, for the ones a reader cannot be expected to know; `null` for a plain range. */
export function egressAddressWords(cidr: string): string | null {
  if (cidr.startsWith(CONTAINER_IP_PLACEHOLDER)) {
    return "the container's own address";
  }
  if (cidr === LINK_LOCAL_RANGE) {
    return "link-local addresses, cloud metadata among them";
  }
  if (cidr === "0.0.0.0/0") return "every address";
  return null;
}

export function egressPortWords(port: string): string {
  if (port === ANY_PORT) return "any port";
  if (port === APPMUX_PORT_PLACEHOLDER) return "appmux's port";
  return `port ${port}`;
}
