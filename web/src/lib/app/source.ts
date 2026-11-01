/**
 * The read-only source viewer (design 9.2, 10.1): the files of one release's
 * snapshot, text only. Each list and each file opened writes an audit row on
 * the server, so nothing here is fetched until the person asks for it. File
 * content is data: it is shown as text and never interpreted.
 */
import type { ReleaseSourceFile, ReleaseSourceListing } from "@/lib/api";
import { assertNever } from "@/lib/assert-never";
import { formatBytes, pluralize } from "@/lib/format";

/** How much of a file is drawn. A larger file is cut and says so; the full file is not offered. */
export const MAX_PREVIEW_CHARS = 200_000;

/** How much of the start of a file is searched for a NUL byte. */
const BINARY_SNIFF_CHARS = 8000;

export const SOURCE_AUDIT_NOTICE =
  "Every list and every file you open is recorded in the audit log.";

// ----------------------------------------------------------------- listing

/**
 * A path from a listing that is safe to ask for: relative, with plain
 * segments, no `.` or `..`, no backslash, no control character. The server
 * checks it again; this keeps a hostile listing from making the page build a
 * request outside the snapshot.
 */
export function isSafeSourcePath(path: string): boolean {
  if (path === "" || path.startsWith("/")) return false;
  if (hasBackslashOrControlCharacter(path)) return false;
  return path
    .split("/")
    .every((segment) => segment !== "" && segment !== "." && segment !== "..");
}

const BACKSLASH_CODE = 0x5c;
const LAST_C0_CONTROL_CODE = 0x1f;
const DELETE_CODE = 0x7f;

function hasBackslashOrControlCharacter(text: string): boolean {
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (
      code === BACKSLASH_CODE ||
      code <= LAST_C0_CONTROL_CODE ||
      code === DELETE_CODE
    ) {
      return true;
    }
  }
  return false;
}

export interface SourceEntry {
  path: string;
  /** The folder part, `""` at the root. */
  directory: string;
  name: string;
  sizeBytes: number;
  kind: "text" | "binary";
  canOpen: boolean;
  /** Why it cannot be opened; `null` when it can. */
  reason: string | null;
}

function toEntry(item: ReleaseSourceListing["items"][number]): SourceEntry {
  const slash = item.path.lastIndexOf("/");
  const base = {
    path: item.path,
    directory: slash === -1 ? "" : item.path.slice(0, slash),
    name: slash === -1 ? item.path : item.path.slice(slash + 1),
    sizeBytes: item.size_bytes,
    kind: item.kind,
  };
  if (item.kind === "binary") {
    return {
      ...base,
      canOpen: false,
      reason: "Binary files are listed, not shown.",
    };
  }
  if (!isSafeSourcePath(item.path)) {
    return {
      ...base,
      canOpen: false,
      reason: "This path cannot be opened from here.",
    };
  }
  return { ...base, canOpen: true, reason: null };
}

/** Sorted by path in code-unit order, so the order does not depend on the browser's locale. */
export function sourceEntries(listing: ReleaseSourceListing): SourceEntry[] {
  return listing.items
    .map(toEntry)
    .toSorted((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

/** "3 files (1 binary), 4 KB". */
export function sourceListingSummary(listing: ReleaseSourceListing): string {
  if (listing.items.length === 0) return "No files";
  const binaryCount = listing.items.filter(
    (item) => item.kind === "binary",
  ).length;
  const totalBytes = listing.items.reduce(
    (total, item) => total + item.size_bytes,
    0,
  );
  const files = pluralize(listing.items.length, "file");
  const binary = binaryCount > 0 ? ` (${binaryCount} binary)` : "";
  return `${files}${binary}, ${formatBytes(totalBytes)}`;
}

// -------------------------------------------------------------------- file

export interface FilePreview {
  /** What to draw: the file, or its first `MAX_PREVIEW_CHARS`; empty for content that looks binary. */
  text: string;
  isCut: boolean;
  isBinaryLooking: boolean;
  /** A sentence about what is not shown; `null` when all of it is. */
  note: string | null;
}

function isHighSurrogate(codeUnit: number): boolean {
  return codeUnit >= 0xd800 && codeUnit <= 0xdbff;
}

export function previewFile(file: ReleaseSourceFile): FilePreview {
  if (file.content.slice(0, BINARY_SNIFF_CHARS).includes("\u0000")) {
    return {
      text: "",
      isCut: false,
      isBinaryLooking: true,
      note: "This file looks binary, so it is not shown.",
    };
  }
  if (file.content.length <= MAX_PREVIEW_CHARS) {
    return {
      text: file.content,
      isCut: false,
      isBinaryLooking: false,
      note: null,
    };
  }
  let end = MAX_PREVIEW_CHARS;
  // Cutting between the halves of a pair would draw a replacement character.
  if (isHighSurrogate(file.content.charCodeAt(end - 1))) end -= 1;
  return {
    text: file.content.slice(0, end),
    isCut: true,
    isBinaryLooking: false,
    note: `Showing the first ${pluralize(end, "character")} of a ${formatBytes(file.size_bytes)} file.`,
  };
}

// ------------------------------------------------------------------- state

export type Loadable<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; value: T }
  | { status: "failed"; error: unknown };

export interface SourceViewState {
  /** The release whose source is open; `null` while the viewer is closed. */
  releaseNo: number | null;
  listing: Loadable<ReleaseSourceListing>;
  /** The file chosen from the listing, if any. */
  filePath: string | null;
  file: Loadable<ReleaseSourceFile>;
}

export const CLOSED_SOURCE_VIEW: SourceViewState = {
  releaseNo: null,
  listing: { status: "idle" },
  filePath: null,
  file: { status: "idle" },
};

export type SourceViewEvent =
  | { type: "open"; releaseNo: number }
  | { type: "close" }
  | { type: "file-closed" }
  | { type: "releases-changed"; releaseNos: readonly number[] }
  | { type: "listing-loaded"; releaseNo: number; listing: ReleaseSourceListing }
  | { type: "listing-failed"; releaseNo: number; error: unknown }
  | { type: "file-requested"; releaseNo: number; path: string }
  | {
      type: "file-loaded";
      releaseNo: number;
      path: string;
      file: ReleaseSourceFile;
    }
  | { type: "file-failed"; releaseNo: number; path: string; error: unknown };

/**
 * An answer counts only for the release and file the person is looking at: a
 * slow answer for the one before is dropped instead of replacing what is on
 * screen.
 */
export function sourceViewReducer(
  state: SourceViewState,
  event: SourceViewEvent,
): SourceViewState {
  switch (event.type) {
    case "open":
      return {
        releaseNo: event.releaseNo,
        listing: { status: "loading" },
        filePath: null,
        file: { status: "idle" },
      };
    case "close":
      return CLOSED_SOURCE_VIEW;
    case "file-closed":
      return { ...state, filePath: null, file: { status: "idle" } };
    case "releases-changed":
      return state.releaseNo !== null &&
        !event.releaseNos.includes(state.releaseNo)
        ? CLOSED_SOURCE_VIEW
        : state;
    case "listing-loaded":
      return event.releaseNo === state.releaseNo
        ? { ...state, listing: { status: "ready", value: event.listing } }
        : state;
    case "listing-failed":
      return event.releaseNo === state.releaseNo
        ? { ...state, listing: { status: "failed", error: event.error } }
        : state;
    case "file-requested":
      return event.releaseNo === state.releaseNo
        ? { ...state, filePath: event.path, file: { status: "loading" } }
        : state;
    case "file-loaded":
      return isCurrentFile(state, event.releaseNo, event.path)
        ? { ...state, file: { status: "ready", value: event.file } }
        : state;
    case "file-failed":
      return isCurrentFile(state, event.releaseNo, event.path)
        ? { ...state, file: { status: "failed", error: event.error } }
        : state;
    default:
      return assertNever(event);
  }
}

function isCurrentFile(
  state: SourceViewState,
  releaseNo: number,
  path: string,
): boolean {
  return state.releaseNo === releaseNo && state.filePath === path;
}
