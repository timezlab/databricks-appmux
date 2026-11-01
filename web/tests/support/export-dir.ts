import assert from "node:assert/strict";
import { existsSync, readdirSync } from "node:fs";
import path from "node:path";

/** The static export `next build` writes (next.config.mjs: `output: "export"`). */
export const EXPORT_DIR = path.resolve(import.meta.dirname, "../../out");

/** Same default as next.config.mjs: the prefix the edge reserves for the admin UI. */
export const BASE_PATH = process.env.APPMUX_BASE_PATH ?? "/_appmux";

/** Every file under the export whose name ends with `extension`, as absolute paths. */
export function exportFiles(extension: string): string[] {
  assert.ok(
    existsSync(EXPORT_DIR),
    `${EXPORT_DIR} does not exist: run "npm run build" before "npm run test:export"`,
  );
  const found: string[] = [];
  const pending = [EXPORT_DIR];
  for (let directory = pending.pop(); directory !== undefined;) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const entryPath = path.join(directory, entry.name);
      if (entry.isDirectory()) pending.push(entryPath);
      else if (entry.name.endsWith(extension)) found.push(entryPath);
    }
    directory = pending.pop();
  }
  return found.toSorted();
}
