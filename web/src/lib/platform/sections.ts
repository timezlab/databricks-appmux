/**
 * The pages of the platform area (design 10.1) and the groups of the settings
 * page. Plain data, so the sidebar, the page titles and the tests read one list.
 */
import type { LucideIcon } from "lucide-react";
import {
  AppWindowIcon,
  KeyRoundIcon,
  LayoutGridIcon,
  NetworkIcon,
  SettingsIcon,
} from "lucide-react";

import type { SettingKey } from "@/lib/api";

/** What `AppSidebar` takes for one item (`SidebarItemData`). */
export interface PlatformSidebarItem {
  label: string;
  /** A literal path of the export; `next/link` adds the base path. */
  href: string;
  icon: LucideIcon;
  /** Left out when there is nothing to do there. */
  count?: number;
  /** What the count means in words, for screen readers and the hover title; present exactly when `count` is. */
  countDescription?: string;
}

/** Where a deleted app's Settings page sends the person: the list of apps. */
export const PLATFORM_APPS_PATH = "/platform/apps/";

const PLATFORM_OVERVIEW_PATH = "/platform/";

/** In sidebar order. The first is the page an admin lands on: what waits for them. */
export const PLATFORM_PAGES: readonly {
  label: string;
  href: string;
  icon: LucideIcon;
}[] = [
  { label: "Overview", href: PLATFORM_OVERVIEW_PATH, icon: LayoutGridIcon },
  { label: "Apps", href: PLATFORM_APPS_PATH, icon: AppWindowIcon },
  { label: "Access", href: "/platform/access/", icon: KeyRoundIcon },
  { label: "Egress", href: "/platform/egress/", icon: NetworkIcon },
  { label: "Settings", href: "/platform/settings/", icon: SettingsIcon },
];

/**
 * The sidebar of the platform area. Overview carries the number of requests
 * that wait for an answer, so an admin on any page sees that the inbox is not
 * empty; there is no count while the request list has not loaded (`null`) or
 * at zero, so a count always means something to do. The count comes with its
 * words, as an app's Requests count does (lib/app/sections.ts), so a screen
 * reader hears "Overview, 3 requests waiting" and not "Overview 3".
 */
export function platformSidebarItems(
  pendingRequests: number | null,
): PlatformSidebarItem[] {
  return PLATFORM_PAGES.map((page) =>
    page.href === PLATFORM_OVERVIEW_PATH &&
    pendingRequests !== null &&
    pendingRequests > 0
      ? {
          ...page,
          count: pendingRequests,
          countDescription:
            pendingRequests === 1
              ? "1 request waiting"
              : `${String(pendingRequests)} requests waiting`,
        }
      : { ...page },
  );
}

export interface SettingGroup {
  id: string;
  /** `#id` of the group's section, for the jump links. */
  href: string;
  title: string;
  summary: string;
  keys: readonly SettingKey[];
}

/**
 * The thirteen settings, grouped by what they govern, riskiest first: the
 * delegation switch lends a token, the rest are limits. Every setting is in
 * exactly one group (tested), so none can go missing from the page.
 */
export const SETTING_GROUPS: readonly SettingGroup[] = [
  {
    id: "delegation",
    href: "#delegation",
    title: "Delegation",
    summary: "Whether apps can be lent the appmux service principal's token.",
    keys: ["shared_sp_delegation_allowed"],
  },
  {
    id: "traffic",
    href: "#traffic",
    title: "Traffic limits",
    summary: "What the edge enforces on each app's requests.",
    keys: [
      "request_timeout_s",
      "max_request_body_mb",
      "max_concurrent_requests",
    ],
  },
  {
    id: "builds",
    href: "#builds",
    title: "Builds and packages",
    summary: "How an Update installs packages and checks what it attaches.",
    keys: [
      "package_mode",
      "default_wheel_sources",
      "build_timeout_s",
      "attach_check_warehouse_id",
    ],
  },
  {
    id: "sandbox",
    href: "#sandbox",
    title: "Sandbox and memory",
    summary: "What each app's sandbox may use, and what appmux keeps back.",
    keys: ["memory_reserve_mb", "max_processes_per_child", "child_cpu_pin"],
  },
  {
    id: "releases",
    href: "#releases",
    title: "Releases",
    summary: "How many releases an app keeps, and how many may be locked.",
    keys: ["max_releases_per_app", "max_locked_releases"],
  },
];
