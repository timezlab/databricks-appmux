/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import { without } from "../../../../support/contract-examples";
import { accepts, rejects, rule } from "../../../../support/schema-assertions";

describe("egress rules", () => {
  const request = (overrides: Record<string, unknown> = {}) => ({
    ...rule(),
    ...overrides,
  });

  it("are strict objects with all four fields", () => {
    accepts(api.EgressRuleRequest, request());
    rejects(api.EgressRuleRequest, request({ is_admin: true }));
    rejects(
      api.EgressRuleRequest,
      without(request(), "comment"),
      "comment is required",
    );
    accepts(api.EgressRuleRequest, request({ comment: "" }));
    accepts(api.PutEgressRequest, { rules: [] });
    rejects(api.PutEgressRequest, { rules: [request({ is_admin: true })] });
    rejects(api.PutEgressRequest, { rules: [], is_admin: true });
    accepts(api.PutAppEgressRequest, { version: 3, rules: [request()] });
    rejects(api.PutAppEgressRequest, { rules: [request()] }, "no version");
    rejects(api.PutAppEgressRequest, {
      version: 3,
      rules: [request({ is_admin: true })],
    });
  });

  it("cidr: a CIDR, or the @container_ip placeholder with an optional prefix length", () => {
    for (const good of [
      "10.20.0.0/16",
      "0.0.0.0/0",
      "192.168.1.1/32",
      "10.20.0.0/15",
      "172.16.0.0/12",
      "@container_ip/32",
      "@container_ip",
    ]) {
      accepts(api.EgressRuleRequest, request({ cidr: good }), good);
    }
    for (const bad of [
      "",
      "10.20.0.0",
      "10.20.0.0/33",
      "300.1.1.1/8",
      "10.20.0.0/-1",
      "@container_ip/33",
      "@appmux_port",
      "container_ip/32",
      "example.com",
      "2001:db8::/32",
      "::/0",
      "10.0.0.1/8",
      "192.168.1.1/24",
      "10.21.0.0/15",
      "010.0.0.0/8",
      "10.0.0.0/",
    ]) {
      rejects(api.EgressRuleRequest, request({ cidr: bad }), bad);
    }
  });

  it("port: any, 1 to 65535, or @appmux_port", () => {
    for (const good of ["any", "1", "443", "65535", "@appmux_port"]) {
      accepts(api.EgressRuleRequest, request({ port: good }), good);
    }
    for (const bad of [
      "",
      "0",
      "65536",
      "-1",
      "01",
      "4 43",
      "80-90",
      "http",
      "ANY",
    ]) {
      rejects(api.EgressRuleRequest, request({ port: bad }), bad);
    }
  });

  it("an allow cannot overlap @container_ip, @appmux_port or 169.254.0.0/16", () => {
    const allow = (overrides: Record<string, unknown>) =>
      request({ action: "allow", ...overrides });
    rejects(api.EgressRuleRequest, allow({ cidr: "@container_ip/32" }));
    rejects(api.EgressRuleRequest, allow({ port: "@appmux_port" }));
    for (const overlapping of [
      "169.254.0.0/16",
      "169.254.169.254/32",
      "169.254.255.255/32",
      "169.254.0.0/32",
      "169.254.1.0/24",
      "169.0.0.0/8",
      "169.254.0.0/15",
      "0.0.0.0/0",
      "0.0.0.0/1",
      "0.0.0.0/8",
      "0.0.0.0/32",
    ]) {
      rejects(api.EgressRuleRequest, allow({ cidr: overlapping }), overlapping);
    }
    for (const clear of [
      "169.253.255.255/32",
      "169.255.0.0/16",
      "10.20.0.0/16",
      "169.253.0.0/16",
      "0.0.0.1/32",
      "1.0.0.0/8",
      "170.0.0.0/7",
    ]) {
      accepts(api.EgressRuleRequest, allow({ cidr: clear }), clear);
    }
  });

  it("a deny may cover the probe address and the placeholders", () => {
    const deny = (cidr: string) => request({ action: "deny", cidr });
    accepts(api.EgressRuleRequest, deny("0.0.0.0/1"));
    accepts(api.EgressRuleRequest, deny("0.0.0.0/32"));
  });

  it("a deny may name them: the default global list does", () => {
    const deny = (overrides: Record<string, unknown>) =>
      request({ action: "deny", ...overrides });
    accepts(
      api.EgressRuleRequest,
      deny({ cidr: "@container_ip/32", port: "any" }),
    );
    accepts(
      api.EgressRuleRequest,
      deny({ cidr: "169.254.0.0/16", port: "any" }),
    );
    accepts(
      api.EgressRuleRequest,
      deny({ cidr: "0.0.0.0/0", port: "@appmux_port" }),
    );
  });
});
