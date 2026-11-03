/**
 * The sidebar of one app's pages: each section's name and icon, where it
 * leads, and the two counts worth showing before a section is opened. Pure, so
 * the counts and the paths are tested without a page.
 */
import type { LucideIcon } from "lucide-react";
import {
  BracesIcon,
  ChartLineIcon,
  DatabaseIcon,
  InboxIcon,
  LayoutGridIcon,
  RocketIcon,
  ScrollTextIcon,
  SettingsIcon,
  ShieldIcon,
  UsersIcon,
} from "lucide-react";

import type { AppDetail, RequestRow } from "@/lib/api";
import { appChangeRequests } from "./requests";
import { APP_SECTIONS, appPagePath } from "./params";
import type { AppSection } from "./params";
import { statusNotices } from "./status";

export const APP_SECTION_LABEL: Readonly<Record<AppSection, string>> = {
  overview: "Overview",
  authorization: "Authorization",
  deployments: "Deployments",
  logs: "Logs",
  security: "Security",
  environment: "Environment",
  resources: "Resources",
  insights: "Insights",
  requests: "Requests",
  settings: "Settings",
};

/** One icon per section, so a sidebar collapsed to icons still tells them apart. */
const APP_SECTION_ICON: Readonly<Record<AppSection, LucideIcon>> = {
  overview: LayoutGridIcon,
  authorization: UsersIcon,
  deployments: RocketIcon,
  logs: ScrollTextIcon,
  security: ShieldIcon,
  environment: BracesIcon,
  resources: DatabaseIcon,
  insights: ChartLineIcon,
  requests: InboxIcon,
  settings: SettingsIcon,
};

/** What `AppSidebar` takes for one item (`SidebarItemData`). */
export interface AppNavItem {
  label: string;
  /** A path from `appPagePath`, so a slug that is not a slug leaves no link. */
  href: string;
  icon: LucideIcon;
  /** Left out when there is nothing to do there. */
  count?: number;
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
 * One item per section of the app. "Overview" carries the number of notices
 * that ask for attention (a crash, a failed release, missing requirements) and
 * "Requests" the number waiting for an answer; the others have no count, and
 * neither has one at zero, so a count always means something to do.
 */
export function appNavItems(
  app: AppDetail,
  requests: readonly RequestRow[] | null,
): AppNavItem[] {
  const counts: Partial<Record<AppSection, number>> = {
    overview: statusNotices(app).length,
    requests:
      requests === null ? 0 : pendingChangeRequestCount(requests, app.slug),
  };
  return APP_SECTIONS.flatMap((section) => {
    const href = appPagePath(app.slug, section);
    if (href === null) return [];
    const item: AppNavItem = {
      label: APP_SECTION_LABEL[section],
      href,
      icon: APP_SECTION_ICON[section],
    };
    const count = counts[section] ?? 0;
    return [count === 0 ? item : { ...item, count }];
  });
}
