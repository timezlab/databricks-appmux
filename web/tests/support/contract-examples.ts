/** Helpers shared by the tests that read the contract's example files. */
import { readFileSync } from "node:fs";
import path from "node:path";

export const CONTRACT_DIR = path.resolve(
  import.meta.dirname,
  "../../../specs/001-appmux-v1/contracts",
);
export const EXAMPLES_DIR = path.join(CONTRACT_DIR, "examples");

export function readJson(filePath: string): unknown {
  return JSON.parse(readFileSync(filePath, "utf8"));
}

/** The contract's valid example of the schema `name`, freshly parsed. */
export function readExample(name: string): unknown {
  return readJson(path.join(EXAMPLES_DIR, `${name}.json`));
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The example as an object with string keys; the examples of objects always are. */
export function readExampleObject(name: string): Record<string, unknown> {
  const example = readExample(name);
  if (!isRecord(example)) throw new Error(`${name} example is not an object`);
  return example;
}

/** A copy of `value` without the property `key`. */
export function without(
  value: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(value).filter(([name]) => name !== key),
  );
}
