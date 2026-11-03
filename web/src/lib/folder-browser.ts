/**
 * What the source-folder browser decides (docs/product/admin-ui-design.md,
 * Components: Folder browser; admin-api.md addendum, `GET /workspace/browse`
 * and `WorkspaceListing`): where it opens, the breadcrumb, the order of the
 * rows, the sentence about a folder that looks like an app, and the words for
 * each refusal of the route. Pure, so it is tested without a popover. The text
 * field beside the browser stays the source of truth: the browser only fills it.
 */
import { ApiError, NETWORK_ERROR } from "@/lib/api";
import type {
  AppSummary,
  MeResponse,
  WorkspaceEntry,
  WorkspaceListing,
  WorkspaceRoot,
} from "@/lib/api";

const WORKSPACE_FOLDER = "/Workspace";
const WORKSPACE_PREFIX = `${WORKSPACE_FOLDER}/`;

/** The first crumb, which lists the roots (My folder, Shared). */
export const START_CRUMB_LABEL = "Workspace";

export interface Crumb {
  label: string;
  /** The folder it opens; `null` for the roots. */
  path: string | null;
}

/**
 * Where the browser opens: the folder the field already names (a settings form
 * holds the app's folder), else the roots. The server judges the rest of the
 * path; a folder that is gone answers `not_found`, whose notice leads back to
 * the start.
 */
export function startPath(fieldText: string): string | null {
  const trimmed = fieldText.trim();
  if (!trimmed.startsWith(WORKSPACE_PREFIX)) return null;
  const folder = trimmed.replace(/\/+$/, "");
  return folder.length > WORKSPACE_FOLDER.length ? folder : null;
}

function isInside(path: string, folder: string): boolean {
  return path === folder || path.startsWith(`${folder}/`);
}

/**
 * The crumbs from the start to `path`. A path under a root the browser has
 * seen is named from that root's label ("My folder"), so the person's own
 * address is not spelled out; any other path is split below `/Workspace`.
 */
export function breadcrumb(
  path: string | null,
  roots: readonly WorkspaceRoot[],
): Crumb[] {
  const start: Crumb = { label: START_CRUMB_LABEL, path: null };
  if (path === null) return [start];
  if (!path.startsWith(WORKSPACE_PREFIX)) return [start, { label: path, path }];
  const root = roots
    .filter((candidate) => isInside(path, candidate.path))
    .toSorted((a, b) => b.path.length - a.path.length)[0];
  const crumbs: Crumb[] = [start];
  let current = root?.path ?? WORKSPACE_FOLDER;
  if (root !== undefined) crumbs.push({ label: root.label, path: root.path });
  for (const segment of path.slice(current.length).split("/")) {
    if (segment === "") continue;
    current = `${current}/${segment}`;
    crumbs.push({ label: segment, path: current });
  }
  return crumbs;
}

/**
 * Where "Up" goes: nowhere from the roots; the parent the server named; the
 * roots when it named none (a root, or a top-level folder such as
 * `/Workspace/Repos`).
 */
export function upTarget(
  listing: WorkspaceListing,
): { path: string | null } | null {
  if (listing.path === null) return null;
  return { path: listing.parent };
}

function byNameIgnoringCase(a: WorkspaceEntry, b: WorkspaceEntry): number {
  const left = a.name.toLowerCase();
  const right = b.name.toLowerCase();
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/**
 * Folders first, then files, each by name without regard to case: the
 * server's order, kept here too so the rows never depend on how a listing
 * arrived.
 */
export function orderEntries(
  entries: readonly WorkspaceEntry[],
): WorkspaceEntry[] {
  return entries.toSorted((a, b) => {
    if (a.kind !== b.kind) return a.kind === "folder" ? -1 : 1;
    return byNameIgnoringCase(a, b);
  });
}

export interface FolderHint {
  text: string;
  /** The budget the hinted runtime needs, offered as "Use N MB"; `null` when there is none to offer. */
  suggestedMemoryMb: number | null;
}

/**
 * The sentence about a folder that looks like an app. Only Node.js comes with
 * a memory figure: the server sends `suggested_memory_mb` for it, the smallest
 * budget it builds and starts with. The runtime is a hint; the Update decides
 * it from the snapshot.
 */
export function folderHint(listing: WorkspaceListing): FolderHint | null {
  if (listing.path === null || !listing.looks_like_app) return null;
  if (listing.runtime_hint === "node" && listing.suggested_memory_mb !== null) {
    const memoryMb = listing.suggested_memory_mb;
    return {
      text: `This folder looks like a Node.js app. Node.js needs about ${memoryMb} MB of memory; with a smaller budget it may fail to build or start.`,
      suggestedMemoryMb: memoryMb,
    };
  }
  if (listing.runtime_hint === "python") {
    return {
      text: "This folder looks like a Python app.",
      suggestedMemoryMb: null,
    };
  }
  return { text: "This folder looks like an app.", suggestedMemoryMb: null };
}

/**
 * What the browser offers after a refusal:
 * - `reload-page`: sign in again (a lapsed session or a missing token);
 * - `retry`: list the same folder again;
 * - `start`: go back to the roots;
 * - `up`: go to the folder above;
 * - `type`: nothing to browse; the field takes the path.
 */
export type BrowseRecovery = "reload-page" | "retry" | "start" | "up" | "type";

export interface BrowseRefusal {
  title: string;
  /** The server's own words, shown as text; `null` when there are none. */
  detail: string | null;
  /** What to do next, one sentence. */
  hint: string;
  recovery: BrowseRecovery;
}

const TYPE_INSTEAD = "You can also type the path in the field.";

type RefusalWords = Omit<BrowseRefusal, "detail">;

const UNLISTED: RefusalWords = {
  title: "The folder could not be listed",
  hint: `Try again. ${TYPE_INSTEAD}`,
  recovery: "retry",
};

/** The route's refusals by code (admin-api.md addendum, `GET /workspace/browse`). */
const REFUSALS = new Map<string, RefusalWords>([
  [
    "session_expired",
    {
      title: "Your Databricks session has expired",
      hint: `Reload the page to sign in again, then browse again. ${TYPE_INSTEAD}`,
      recovery: "reload-page",
    },
  ],
  [
    "user_token_missing",
    {
      title: "appmux did not receive your Databricks token",
      hint: `Reload the page. If it happens again, tell a platform admin that appmux cannot act for you in the workspace. ${TYPE_INSTEAD}`,
      recovery: "reload-page",
    },
  ],
  [
    "no_access",
    {
      title: "Databricks does not let you open this folder",
      hint: "Go up and pick another folder, or ask the folder's owner for access.",
      recovery: "up",
    },
  ],
  [
    "not_found",
    {
      title: "This folder does not exist, or you cannot see it",
      hint: `Go back to the start and pick a folder. ${TYPE_INSTEAD}`,
      recovery: "start",
    },
  ],
  [
    "workspace_unavailable",
    {
      title: "The workspace did not answer",
      hint: `Try again in a moment. ${TYPE_INSTEAD}`,
      recovery: "retry",
    },
  ],
  [
    "invalid_path",
    {
      title: "That is not a workspace folder",
      hint: "A folder path starts with /Workspace/. Go back to the start, or fix the path in the field.",
      recovery: "start",
    },
  ],
  [
    "forbidden",
    {
      title: "Browsing is for platform admins and app admins",
      hint: "Type the path in the field instead.",
      recovery: "type",
    },
  ],
]);

/** One refusal of `GET /workspace/browse` in words, by its code. */
export function browseRefusal(error: unknown): BrowseRefusal {
  if (!(error instanceof ApiError)) return { ...UNLISTED, detail: null };
  if (error.code === NETWORK_ERROR) {
    return {
      title: "appmux did not answer",
      detail: null,
      hint: `Check the connection and try again. ${TYPE_INSTEAD}`,
      recovery: "retry",
    };
  }
  return { ...(REFUSALS.get(error.code) ?? UNLISTED), detail: error.message };
}

/**
 * Whether to offer the browser: the route answers a platform admin and a
 * person with `CAN_MANAGE` on at least one app, and `403` to anyone else (a
 * plain visitor's request form), who types the path. `false` while either
 * answer is missing, so no button leads to a refusal.
 */
export function mayBrowseWorkspace(
  me: MeResponse | null,
  apps: readonly AppSummary[] | null,
): boolean {
  if (me === null) return false;
  if (me.is_platform_admin) return true;
  if (apps === null) return false;
  return apps.some((app) => app.permission === "CAN_MANAGE");
}
