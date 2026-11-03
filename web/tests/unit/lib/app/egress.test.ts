import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { EgressRule } from "@/lib/api/schemas";
import {
  appEgressMode,
  egressAddressWords,
  egressPortWords,
} from "@/lib/app/egress";

function rule(
  action: EgressRule["action"],
  cidr: string,
  port = "any",
): EgressRule {
  return { action, cidr, port, comment: "" };
}

const DENY_ALL = rule("deny", "0.0.0.0/0");

describe("appEgressMode", () => {
  it("calls an app with no rules of its own pass-through: the global rules decide", () => {
    assert.deepEqual(appEgressMode([]), { kind: "pass-through" });
  });

  it("calls allow rules closed by a deny of everything an allow-list, and counts what it allows", () => {
    assert.deepEqual(
      appEgressMode([
        rule("allow", "10.20.0.0/16", "443"),
        rule("allow", "10.30.0.0/16"),
        DENY_ALL,
      ]),
      { kind: "allow-list", allowed: 2 },
    );
  });

  it("calls a deny of everything alone closed: nothing past 10.0.2.0/24 is reachable", () => {
    assert.deepEqual(appEgressMode([DENY_ALL]), { kind: "closed" });
  });

  it("calls any other list its own rules, which the global ones follow", () => {
    assert.deepEqual(appEgressMode([rule("deny", "10.40.0.0/16")]), {
      kind: "rules",
      count: 1,
    });
    // A deny of everything on one port only still lets the rest through.
    assert.deepEqual(
      appEgressMode([
        rule("allow", "10.20.0.0/16"),
        rule("deny", "0.0.0.0/0", "443"),
      ]),
      { kind: "rules", count: 2 },
    );
  });
});

describe("egressAddressWords and egressPortWords", () => {
  it("name the placeholders and the ranges the default list is about", () => {
    assert.equal(
      egressAddressWords("@container_ip/32"),
      "the container's own address",
    );
    assert.equal(
      egressAddressWords("169.254.0.0/16"),
      "link-local addresses, cloud metadata among them",
    );
    assert.equal(egressAddressWords("0.0.0.0/0"), "every address");
    assert.equal(egressAddressWords("10.20.0.0/16"), null);
  });

  it("say any port, appmux's port, or the port", () => {
    assert.equal(egressPortWords("any"), "any port");
    assert.equal(egressPortWords("@appmux_port"), "appmux's port");
    assert.equal(egressPortWords("443"), "port 443");
  });
});
