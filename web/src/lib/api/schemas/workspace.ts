/** GET /workspace/browse: one workspace folder, for the source-folder field's browser. */
import { z } from "./zod.ts";
import { Runtime } from "./enums.ts";

/** The files whose presence says what a folder is (admin-api.md, WorkspaceListing.markers). */
export const WorkspaceMarker = z.enum([
  "app.yaml",
  "databricks.yml",
  "requirements.txt",
  "package.json",
  "pyproject.toml",
]);

export type WorkspaceMarker = z.infer<typeof WorkspaceMarker>;

export const WorkspaceRoot = z.object({
  label: z.string(),
  path: z.string(),
});

export type WorkspaceRoot = z.infer<typeof WorkspaceRoot>;

export const WorkspaceEntry = z.object({
  name: z.string(),
  path: z.string(),
  kind: z.enum(["folder", "file"]),
});

export type WorkspaceEntry = z.infer<typeof WorkspaceEntry>;

/**
 * `path` is null for the roots, which are then in `roots` (else `[]`). `parent` is null at a root
 * and at a top-level folder, so the browser goes back to the roots.
 */
export const WorkspaceListing = z.object({
  path: z.string().nullable(),
  parent: z.string().nullable(),
  roots: z.array(WorkspaceRoot),
  entries: z.array(WorkspaceEntry),
  truncated: z.boolean(),
  markers: z.array(WorkspaceMarker),
  looks_like_app: z.boolean(),
  runtime_hint: Runtime.nullable(),
  suggested_memory_mb: z.int().nullable(),
});

export type WorkspaceListing = z.infer<typeof WorkspaceListing>;
