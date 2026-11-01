/** Assertions and fixtures that the schema-limit tests share. */
import assert from "node:assert/strict";
import type { z } from "zod";

export function accepts(schema: z.ZodType, value: unknown, label = ""): void {
  const result = schema.safeParse(value);
  assert.ok(
    result.success,
    `${label} should parse: ${result.success ? "" : result.error.message}`,
  );
}

export function rejects(schema: z.ZodType, value: unknown, label = ""): void {
  assert.equal(
    schema.safeParse(value).success,
    false,
    `${label} should be refused`,
  );
}

/** A valid body with `override` merged over it. */
export function variant(
  base: Record<string, unknown>,
  override: Record<string, unknown>,
): Record<string, unknown> {
  return { ...base, ...override };
}

/** A valid egress rule body. */
export function rule(): Record<string, string> {
  return { action: "deny", cidr: "10.20.0.0/16", port: "443", comment: "" };
}
