/**
 * The tabs of one app's pages: their names, where they lead, and the two counts
 * worth showing before a tab is opened. Pure, so the counts and the paths are
 * tested without a page.
 */
import type { AppDetail, RequestRow } from "@/lib/api";
import { pluralize } from "@/lib/format";
import { appChangeRequests } from "./requests";
import { APP_SECTIONS, appPagePath } from "./params";
import type { AppSection } from "./params";
import { statusNotices } from "./status";

export const APP_SECTION_LABEL: Readonly<Record<AppSection, string>> = {
  status: "Status",
  releases: "Releases",
  resources: "Resources",
  sharing: "Sharing",
  requests: "Requests",
};

export interface AppNavItem {
  label: string;
  /** A path from `appPagePath`, so a slug that is not a slug leaves no link. */
  href: string;
  badge: { text: string; description: string } | null;
}

/** Requests of this app that wait for an answer (settings and delegation; the register request is the launcher's). */
export function pendingChangeRequestCount(
  requests: readonly RequestRow[],
  slug: string,
): number {
  return appChangeRequests(requests, slug).filter(
    (request) => request.status === "pending",
  ).length;
}

/**
 * One item per page of the app. "Status" carries the number of notices that
 * ask for attention (a crash, a failed release, missing requirements) and
 * "Requests" the number waiting for an answer; the others have no badge, so a
 * badge always means something to do.
 */
export function appNavItems(
  app: AppDetail,
  requests: readonly RequestRow[] | null,
): AppNavItem[] {
  const attention = statusNotices(app).length;
  const waiting =
    requests === null ? 0 : pendingChangeRequestCount(requests, app.slug);
  const badges: Partial<Record<AppSection, AppNavItem["badge"]>> = {
    status:
      attention === 0
        ? null
        : {
            text: String(attention),
            description: pluralize(
              attention,
              "thing needs attention",
              "things need attention",
            ),
          },
    requests:
      waiting === 0
        ? null
        : { text: String(waiting), description: `${waiting} waiting` },
  };
  return APP_SECTIONS.flatMap((section) => {
    const href = appPagePath(app.slug, section);
    return href === null
      ? []
      : [
          {
            label: APP_SECTION_LABEL[section],
            href,
            badge: badges[section] ?? null,
          },
        ];
  });
}
