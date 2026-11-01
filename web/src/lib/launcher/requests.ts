/**
 * The requests half of the launcher (design 9.4, admin-api.md `RequestList`):
 * the visitor's own requests with their state, the platform admin's pending
 * count, and what a poll changes. The server sends every request the actor may
 * see; this module builds the launcher's views from that one list.
 */
import type {
  MeResponse,
  RequestList,
  RequestRow,
  RequestStatus,
} from "@/lib/api";
import {
  UNKNOWN_TEXT,
  formatCount,
  formatMegabytes,
  pluralize,
} from "@/lib/format";
import type { PollResult } from "@/lib/poll/schedule";
import { inboxRequests, ownRequests } from "@/lib/request-views";

/** FR-041 and the `register` catalogue entry. The server refuses a fourth; the form says so before sending. */
export const MAX_PENDING_REGISTER_REQUESTS = 3;
/** Requests stay in the list for good (TD-007), so only this many answered ones are drawn until the person asks for more. */
export const RECENT_CLOSED_REQUESTS_SHOWN = 10;
/** The inbox section of the platform page. The page is a separate screen (T174). */
export const PLATFORM_INBOX_HREF = "/platform/#inbox";

const MAX_ANSWERS_ANNOUNCED = 3;

// ------------------------------------------------------------- one request

export interface RequestDescription {
  /** The one line that says what was asked. */
  summary: string;
  /** "new app wiki": completes "Your request for ...". */
  shortLabel: string;
  /** One line per field or flag asked; plain text. */
  details: readonly string[];
}

const FIELD_LABEL: Readonly<Record<string, string>> = {
  slug: "Slug",
  display_name: "Name",
  source_path: "Source folder",
  memory_budget_mb: "Memory budget",
};
// The order of the catalogue (data model 4.3); a key the UI does not know follows, sorted.
const FIELD_ORDER = ["slug", "display_name", "source_path", "memory_budget_mb"];

const DELEGATION_DETAIL: Readonly<Record<string, string>> = {
  obo: "OBO: pass each visitor's token to the app",
  shared_sp: "Shared service principal: lend its token to the app",
};

/** A payload value as text. An object or a null is "unknown": its dump is neither readable nor needed. */
function valueText(key: string, value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number") {
    return key === "memory_budget_mb"
      ? formatMegabytes(value)
      : formatCount(value);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  return UNKNOWN_TEXT;
}

function orderedKeys(payload: Readonly<Record<string, unknown>>): string[] {
  const known = FIELD_ORDER.filter((key) => key in payload);
  const others = Object.keys(payload)
    .filter((key) => !FIELD_ORDER.includes(key))
    .toSorted();
  return [...known, ...others];
}

/** The app the request is about: its row, or for a pending `register` the slug it proposes. */
function slugOf(request: RequestRow): string {
  const proposed = request.payload.slug;
  return (
    request.app_slug ?? (typeof proposed === "string" ? proposed : UNKNOWN_TEXT)
  );
}

function registerDetails(request: RequestRow): string[] {
  return ["slug", "source_path", "memory_budget_mb"]
    .filter((key) => key in request.payload)
    .map(
      (key) =>
        `${FIELD_LABEL[key] ?? key}: ${valueText(key, request.payload[key])}`,
    );
}

function settingsDetails(request: RequestRow): string[] {
  return orderedKeys(request.payload).map((key) => {
    const label = FIELD_LABEL[key] ?? key;
    const requested = valueText(key, request.payload[key]);
    const current = request.current;
    if (current === null || !(key in current)) return `${label}: ${requested}`;
    return `${label}: ${valueText(key, current[key])} → ${requested}`;
  });
}

function delegationDetails(request: RequestRow): string[] {
  return Object.keys(request.payload)
    .toSorted()
    .filter((key) => request.payload[key] === true)
    .map((key) => DELEGATION_DETAIL[key] ?? key);
}

export function describeRequest(request: RequestRow): RequestDescription {
  const slug = slugOf(request);
  switch (request.kind) {
    case "register": {
      const name = request.payload.display_name;
      return {
        summary:
          typeof name === "string" && name !== ""
            ? `New app: ${name}`
            : `New app: ${slug}`,
        shortLabel: `new app ${slug}`,
        details: registerDetails(request),
      };
    }
    case "delegation":
      return {
        summary: `Delegation for ${slug}`,
        shortLabel: `delegation for ${slug}`,
        details: delegationDetails(request),
      };
    case "settings":
      return {
        summary: `Settings change for ${slug}`,
        shortLabel: `settings change for ${slug}`,
        details: settingsDetails(request),
      };
  }
}

/** Only the asker withdraws, and only while the request is pending (design 9.4). */
export function canWithdraw(request: RequestRow, actorEmail: string): boolean {
  return request.status === "pending" && request.requested_by === actorEmail;
}

export function withdrawnMessage(request: RequestRow): string {
  return `Withdrew your request for ${describeRequest(request).shortLabel}.`;
}

/** What the platform admin wrote when answering; nothing while pending or after a withdrawal. */
export function reviewerNote(request: RequestRow): string | null {
  const answered: readonly RequestStatus[] = ["approved", "denied"];
  if (!answered.includes(request.status)) return null;
  const note = request.closed_note?.trim() ?? "";
  return note === "" ? null : note;
}

// --------------------------------------------------------------- own views

export interface OwnRequestsView {
  rows: RequestRow[];
  /** Answered requests left out; zero when everything is shown. */
  hiddenCount: number;
}

/**
 * The visitor's own requests, newest first. Every pending request is shown; the
 * answered ones are cut to the newest `RECENT_CLOSED_REQUESTS_SHOWN` unless
 * `showAll`, because `GET /requests` returns the whole history.
 */
export function visibleOwnRequests(
  items: readonly RequestRow[],
  actorEmail: string,
  showAll: boolean,
): OwnRequestsView {
  const own = ownRequests(items, actorEmail);
  if (showAll) return { rows: own, hiddenCount: 0 };
  let closedShown = 0;
  const rows = own.filter((request) => {
    if (request.status === "pending") return true;
    if (closedShown >= RECENT_CLOSED_REQUESTS_SHOWN) return false;
    closedShown += 1;
    return true;
  });
  return { rows, hiddenCount: own.length - rows.length };
}

/**
 * The label of the button that shows more or fewer answered requests, or
 * `null` when there is nothing to toggle. While everything is shown nothing is
 * hidden, so the way back is offered on `showAll`, not on `hiddenCount`.
 */
export function olderAnswersToggleLabel(
  hiddenCount: number,
  showAll: boolean,
): string | null {
  if (showAll) return "Show only the latest answers";
  return hiddenCount > 0 ? `Show ${hiddenCount} older answered` : null;
}

/** Proposed slugs of the pending `register` requests among `rows` (pass the visitor's own). */
export function pendingRegisterSlugs(rows: readonly RequestRow[]): string[] {
  return rows.flatMap((request) => {
    const slug = request.payload.slug;
    return request.kind === "register" &&
      request.status === "pending" &&
      typeof slug === "string"
      ? [slug]
      : [];
  });
}

// ------------------------------------------------------------ platform admin

/** The number of pending requests, or `null` for a visitor or while it is not known. */
export function adminPendingCount(
  me: PollResult<MeResponse>,
  requests: PollResult<RequestList>,
): number | null {
  if (me.status === "loading" || me.status === "failed") return null;
  if (!me.data.is_platform_admin) return null;
  if (requests.status === "loading" || requests.status === "failed")
    return null;
  return inboxRequests(requests.data.items).length;
}

export function pendingCountText(count: number): string {
  if (count === 0) return "No requests are waiting for a decision.";
  return `${pluralize(count, "request")} ${count === 1 ? "is" : "are"} waiting for a decision.`;
}

/**
 * The banner on the launcher: only a platform admin with something waiting sees
 * one. An empty inbox needs no announcement, and the inbox stays one click away
 * on the Platform page.
 */
export function pendingBannerText(count: number | null): string | null {
  return count === null || count === 0 ? null : pendingCountText(count);
}

// ------------------------------------------------------------ what a poll changes

/**
 * A sentence for the screen reader when a poll shows that one of the person's
 * pending requests was approved or denied since the last poll (there is no
 * notification outside the UI in v1, design 9.4). `null` on the first load,
 * when nothing was answered, or when the person withdrew it themselves.
 */
export function answeredRequestsMessage(
  previous: readonly RequestRow[] | null,
  next: readonly RequestRow[],
  actorEmail: string,
): string | null {
  if (previous === null) return null;
  const wasPending = new Set(
    previous
      .filter((request) => request.status === "pending")
      .map((request) => request.request_id),
  );
  const answered = ownRequests(next, actorEmail).filter(
    (request) =>
      wasPending.has(request.request_id) &&
      (request.status === "approved" || request.status === "denied"),
  );
  if (answered.length === 0) return null;

  const sentences = answered.slice(0, MAX_ANSWERS_ANNOUNCED).map((request) => {
    const label = describeRequest(request).shortLabel;
    const note = reviewerNote(request);
    return request.status === "denied" && note !== null
      ? `Your request for ${label} was denied: ${note}.`
      : `Your request for ${label} was ${request.status}.`;
  });
  const rest = answered.length - sentences.length;
  if (rest > 0) sentences.push(`${rest} more answered.`);
  return sentences.join(" ");
}

// -------------------------------------------------------- the section's state

/** What "Your requests" can show, from the two answers it depends on (who am I, and the list). */
export type OwnRequestsSection =
  | { status: "loading" }
  | { status: "failed"; error: unknown }
  | {
      status: "ready";
      email: string;
      items: readonly RequestRow[];
      /** A later poll failed; the data above is the last good one. */
      stale: { error: unknown } | null;
    };

export function ownRequestsSection(
  me: PollResult<MeResponse>,
  requests: PollResult<RequestList>,
): OwnRequestsSection {
  if (me.status === "failed") return { status: "failed", error: me.error };
  if (requests.status === "failed") {
    return { status: "failed", error: requests.error };
  }
  if (me.status === "loading" || requests.status === "loading") {
    return { status: "loading" };
  }
  const staleError =
    requests.status === "stale"
      ? { error: requests.error }
      : me.status === "stale"
        ? { error: me.error }
        : null;
  return {
    status: "ready",
    email: me.data.email,
    items: requests.data.items,
    stale: staleError,
  };
}

/**
 * "Your requests" is on the page only for a person who has asked for something.
 * While the answers load it stays out (a flash of a section that then goes away
 * is worse than a section that arrives); a failure stays visible, because the
 * person might have requests the page could not load.
 */
export function shouldShowOwnRequests(section: OwnRequestsSection): boolean {
  switch (section.status) {
    case "loading":
      return false;
    case "failed":
      return true;
    case "ready":
      return ownRequests(section.items, section.email).length > 0;
  }
}
