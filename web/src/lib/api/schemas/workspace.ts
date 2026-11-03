/** GET /workspace/browse: one workspace folder, for the source-folder field's browser. */
import { z } from "./zod.ts";
import { Runtime } from "./enums.ts";
import { plainText } from "./primitives.ts";

/** admin-api.md addendum, GET /workspace/browse: a longer path is `400` `invalid_path`. */
const PATH_MAX_LENGTH = 512;

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
 * `path` is null for the roots, which are then in `roots` (else `[]`). `parent` is the real folder
 * above, down to `/Workspace`, which has none.
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

/**
 * The query string of `GET /workspace/browse`. It is no body of the contract, so
 * it has no example file; `browseWorkspace` checks it before it builds the URL.
 * Without `path` the server lists the roots. That the path starts with
 * `/Workspace/` and has no empty, `.` or `..` segment is the server's rule
 * (`400` `invalid_path`): the client refuses only a path that can never be sent.
 */
export const WorkspaceBrowseQuery = z.strictObject({
  path: plainText(PATH_MAX_LENGTH).optional(),
});

export type WorkspaceBrowseQuery = z.infer<typeof WorkspaceBrowseQuery>;
