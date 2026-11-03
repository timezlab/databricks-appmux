/** Every contract example parses with its schema, and the examples agree with each other. */
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";
import { z } from "zod";

import * as api from "@/lib/api";
import {
  EXAMPLES_DIR,
  isRecord,
  readExample,
  readJson,
} from "../support/contract-examples";
import { manifest } from "../support/manifest";

/** Optional keys of the request schemas: removing one must still parse. */
const OPTIONAL_REQUEST_KEYS: Record<string, string[]> = {
  RegisterAppRequest: ["memory_budget_mb"],
  UpdateAppRequest: ["slug", "display_name", "source_path", "memory_budget_mb"],
  AttachResourceRequest: ["from_bundle"],
  DecidePermissionRequest: ["reason", "note"],
};

/** Fields whose content is free-form or optional, so deep removal proves nothing. */
const FREE_FORM_KEYS = new Set([
  "payload",
  "current",
  "detail",
  "egress_dropped_by_rule",
  "value",
  "value_from",
]);

function schemaNamed(name: string): z.ZodType {
  const candidate = (api as Record<string, unknown>)[name];
  assert.ok(
    candidate instanceof z.ZodType,
    `${name} is not exported as a Zod schema`,
  );
  return candidate;
}

/** Every path to an object property below the root, skipping free-form fields. */
function propertyPaths(value: unknown, prefix: string[] = []): string[][] {
  if (Array.isArray(value)) {
    return value.length > 0 ? propertyPaths(value[0], [...prefix, "0"]) : [];
  }
  if (!isRecord(value)) return [];
  return Object.entries(value).flatMap(([key, child]) => {
    if (FREE_FORM_KEYS.has(key)) return [];
    return [[...prefix, key], ...propertyPaths(child, [...prefix, key])];
  });
}

/** A copy of `value` without the property at `propertyPath` ("0" is the first array element). */
function withoutPath(value: unknown, propertyPath: string[]): unknown {
  const [head, ...rest] = propertyPath;
  if (head === undefined) return value;
  if (Array.isArray(value)) {
    return value.map((element: unknown, index) =>
      index === Number(head) ? withoutPath(element, rest) : element,
    );
  }
  assert.ok(isRecord(value), `no object at ${head}`);
  if (rest.length === 0) {
    return Object.fromEntries(
      Object.entries(value).filter(([key]) => key !== head),
    );
  }
  return { ...value, [head]: withoutPath(value[head], rest) };
}

/** Paths to every object below the root of `value` that is not free-form JSON. */
function nestedObjectPaths(value: unknown, prefix: string[] = []): string[][] {
  const children: [string, unknown][] = Array.isArray(value)
    ? value.map((element: unknown, index) => [String(index), element])
    : isRecord(value)
      ? Object.entries(value)
      : [];
  return children.flatMap(([key, child]) => {
    if (key === "value" && prefix.length === 0) return []; // any JSON
    const childPath = [...prefix, key];
    return [
      ...(isRecord(child) ? [childPath] : []),
      ...nestedObjectPaths(child, childPath),
    ];
  });
}

/** A copy of `value` with `is_admin: true` added to the object at `objectPath`. */
function withUnlistedField(value: unknown, objectPath: string[]): unknown {
  const [head, ...rest] = objectPath;
  if (head === undefined) {
    assert.ok(isRecord(value));
    return { ...value, is_admin: true };
  }
  if (Array.isArray(value)) {
    return value.map((element: unknown, index) =>
      index === Number(head) ? withUnlistedField(element, rest) : element,
    );
  }
  assert.ok(isRecord(value));
  return { ...value, [head]: withUnlistedField(value[head], rest) };
}

describe("contract examples", () => {
  const entries = Object.entries(manifest);

  it("lists every example file, and only those", () => {
    const onDisk = readdirSync(EXAMPLES_DIR)
      .filter((name) => name !== "manifest.json")
      .sort();
    const listed = entries.map(([, entry]) => entry.file).sort();
    assert.deepEqual(listed, onDisk);
  });

  for (const [name, entry] of entries) {
    const example = readJson(path.join(EXAMPLES_DIR, entry.file));

    describe(name, () => {
      it("parses with the schema of the same name", () => {
        const result = schemaNamed(name).safeParse(example);
        assert.ok(
          result.success,
          result.success ? "" : z.prettifyError(result.error),
        );
      });

      it("fails when a top-level field is removed, unless the field is optional", () => {
        assert.ok(isRecord(example), "every example is an object");
        const optional = OPTIONAL_REQUEST_KEYS[name] ?? [];
        for (const key of Object.keys(example)) {
          const mutated = withoutPath(example, [key]);
          const parses = schemaNamed(name).safeParse(mutated).success;
          assert.equal(parses, optional.includes(key), `removing ${key}`);
        }
      });

      it("fails when a top-level field has the wrong type", () => {
        assert.ok(isRecord(example), "every example is an object");
        for (const key of Object.keys(example)) {
          const mutated: Record<string, unknown> = {
            ...example,
            [key]: Symbol("not json"),
          };
          assert.equal(
            schemaNamed(name).safeParse(mutated).success,
            false,
            `retyping ${key}`,
          );
        }
      });

      if (entry.direction === "request") {
        it("rejects a field the schema does not list", () => {
          const mutated = {
            ...(example as Record<string, unknown>),
            is_admin: true,
          };
          assert.equal(schemaNamed(name).safeParse(mutated).success, false);
        });

        it("rejects a field the schema does not list in any nested object", () => {
          for (const objectPath of nestedObjectPaths(example)) {
            const mutated = withUnlistedField(example, objectPath);
            assert.equal(
              schemaNamed(name).safeParse(mutated).success,
              false,
              `adding is_admin at ${objectPath.join(".")}`,
            );
          }
        });
      } else if (name !== "ErrorBody") {
        it("fails when any nested field is removed", () => {
          for (const propertyPath of propertyPaths(example)) {
            const mutated = withoutPath(example, propertyPath);
            assert.equal(
              schemaNamed(name).safeParse(mutated).success,
              false,
              `removing ${propertyPath.join(".")}`,
            );
          }
        });
      }
    });
  }
});

describe("contract examples agree with each other", () => {
  it("PlatformApp carries the fields of the same app's AppDetail", () => {
    const detail = api.AppDetail.parse(readExample("AppDetail"));
    const platformApp = api.PlatformApp.parse(readExample("PlatformApp"));
    assert.deepEqual(platformApp, {
      slug: detail.slug,
      display_name: detail.display_name,
      source_path: detail.source_path,
      memory_budget_mb: detail.memory_budget_mb,
      owner: detail.owner,
      obo_delegated: detail.delegation.obo_delegated,
      shared_sp_delegated: detail.delegation.shared_sp_delegated,
      egress: detail.egress,
      version: detail.version,
    });
  });

  it("PlatformView.apps lists PlatformApp rows ordered by display_name, then slug", () => {
    const { apps } = api.PlatformView.parse(readExample("PlatformView"));
    const sales = apps.find((app) => app.slug === "sales");
    assert.deepEqual(sales, api.PlatformApp.parse(readExample("PlatformApp")));
    const keys = apps.map((app) => `${app.display_name}\u0000${app.slug}`);
    assert.deepEqual(keys, [...keys].sort());
  });

  it("AppList is ordered by display_name, then slug", () => {
    const { items } = api.AppList.parse(readExample("AppList"));
    const keys = items.map((app) => `${app.display_name}\u0000${app.slug}`);
    assert.deepEqual(keys, [...keys].sort());
  });

  it("MetricsResponse holds at most 10000 rows", () => {
    const { items } = api.MetricsResponse.parse(readExample("MetricsResponse"));
    assert.ok(items.length <= 10000);
  });

  it("an approval answers the new apps version; no other row carries one", () => {
    assert.equal(
      api.RequestRow.parse(readExample("RequestRow")).app_version,
      null,
    );
    const approved = {
      ...api.RequestRow.parse(readExample("RequestRow")),
      app_version: 4,
    };
    assert.equal(api.RequestRow.parse(approved).app_version, 4);
  });
});
