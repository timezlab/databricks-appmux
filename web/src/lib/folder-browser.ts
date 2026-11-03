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

/** The top of the tree: it lists Users, Shared and Repos, and has nothing above it. */
export const WORKSPACE_FOLDER = "/Workspace";
const WORKSPACE_PREFIX = `${WORKSPACE_FOLDER}/`;

/** The first crumb, which opens the top of the tree. */
export const START_CRUMB_LABEL = "Workspace";

export interface Crumb {
  label: string;
  /** The folder it opens. */
  path: string;
}

/**
 * Where the browser opens: the folder the field already names (a settings form
 * holds the app's folder); `null` when the field names none, and the browser
 * then opens at the person's own folder, the first of the roots. The server
 * judges the rest of the path; a folder that is gone answers `not_found`,
 * whose notice leads to the top of the tree.
 */
export function startPath(fieldText: string): string | null {
  const trimmed = fieldText.trim();
  if (!trimmed.startsWith(WORKSPACE_PREFIX)) return null;
  const folder = trimmed.replace(/\/+$/, "");
  return folder.length > WORKSPACE_FOLDER.length ? folder : null;
}

/**
 * The crumbs from `/Workspace` to `path`, one per folder, so every crumb and
 * "Up" lead to a real folder, another person's included. A folder that is one
 * of the roots the browser has seen is named by its label ("My folder"), so the
 * person's own address is not spelled out; every other folder shows its name.
 * `null` (the start is not known yet) has only the top.
 */
export function breadcrumb(
  path: string | null,
  roots: readonly WorkspaceRoot[],
): Crumb[] {
  const crumbs: Crumb[] = [
    { label: START_CRUMB_LABEL, path: WORKSPACE_FOLDER },
  ];
  if (path?.startsWith(WORKSPACE_PREFIX) !== true) return crumbs;
  let current = WORKSPACE_FOLDER;
  for (const segment of path.slice(WORKSPACE_PREFIX.length).split("/")) {
    if (segment === "") continue;
    current = `${current}/${segment}`;
    const root = roots.find((candidate) => candidate.path === current);
    crumbs.push({ label: root?.label ?? segment, path: current });
  }
  return crumbs;
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
 * - `start`: go to the top of the tree (`/Workspace`), which always exists;
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
      hint: `Go to the top of the Workspace and pick a folder. ${TYPE_INSTEAD}`,
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
      hint: "A folder path starts with /Workspace/. Go to the top of the Workspace, or fix the path in the field.",
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
