/** IPv4 CIDR and port rules of the egress contract; pure, no Zod. */
export const MAX_PORT = 65535;

/** Spellings of the egress rule contract (admin-api.md, EgressRule); the egress editor imports them. */
export const CONTAINER_IP_PLACEHOLDER = "@container_ip";

export const APPMUX_PORT_PLACEHOLDER = "@appmux_port";

export const ANY_PORT = "any";

/** The link-local range (metadata addresses) that an `allow` may not overlap. */
export const LINK_LOCAL_RANGE = "169.254.0.0/16";

/** `@container_ip`, optionally with a prefix length (`@container_ip/32`). */
export const CONTAINER_IP_CIDR_PATTERN =
  /^@container_ip(\/([0-9]|[12][0-9]|3[0-2]))?$/;

export function isValidEgressPort(port: string): boolean {
  if (port === ANY_PORT || port === APPMUX_PORT_PLACEHOLDER) return true;
  return /^[1-9][0-9]{0,4}$/.test(port) && Number(port) <= MAX_PORT;
}

const LINK_LOCAL_FIRST = 0xa9fe0000;

// 169.254.0.0
const LINK_LOCAL_LAST = 0xa9feffff;

// 169.254.255.255

// The server checks a list against this address (`_PROBE_CONTAINER_IP` in
// admin/schemas.py): an `allow` that holds it is refused as overlapping
// `@container_ip`, wherever the container really is.
const PROBE_ADDRESS = 0;

/** Octets without a leading zero, as `ipaddress.IPv4Network` reads them; one or two digits of prefix length. */
const IPV4_CIDR_PATTERN =
  /^(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\/(\d{1,2})$/;

type Ipv4Cidr =
  | { kind: "ok"; first: number; last: number }
  | { kind: "malformed" }
  | { kind: "host-bits" };

/** First and last address of an IPv4 CIDR, or why `cidr` is not one the server accepts. */
export function parseIpv4Cidr(cidr: string): Ipv4Cidr {
  const match = IPV4_CIDR_PATTERN.exec(cidr);
  if (match === null) return { kind: "malformed" };
  const octets = match.slice(1, 5).map(Number);
  const prefixLength = Number(match[5]);
  if (octets.some((octet) => octet > 255) || prefixLength > 32) {
    return { kind: "malformed" };
  }
  const address = octets.reduce((total, octet) => total * 256 + octet, 0) >>> 0;
  // A JavaScript shift count wraps at 32, so /32 (no host bits) is spelled out.
  const hostMask = prefixLength === 32 ? 0 : 0xffffffff >>> prefixLength;
  if ((address & hostMask) !== 0) return { kind: "host-bits" };
  return { kind: "ok", first: address, last: (address | hostMask) >>> 0 };
}

export function overlapsLinkLocal(cidr: string): boolean {
  const range = parseIpv4Cidr(cidr);
  return (
    range.kind === "ok" &&
    range.first <= LINK_LOCAL_LAST &&
    range.last >= LINK_LOCAL_FIRST
  );
}

export function holdsProbeAddress(cidr: string): boolean {
  const range = parseIpv4Cidr(cidr);
  return (
    range.kind === "ok" &&
    range.first <= PROBE_ADDRESS &&
    range.last >= PROBE_ADDRESS
  );
}
