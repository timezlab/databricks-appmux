/**
 * The pages of the platform area (design 10.1) and the groups of the settings
 * page. Plain data, so the tab bar, the page titles and the tests read one list.
 */
import type { SettingKey } from "@/lib/api";
import { pluralize } from "@/lib/format";

export interface PlatformNavItem {
  label: string;
  /** A literal path of the export; `next/link` adds the base path. */
  href: string;
  badge: { text: string; description: string } | null;
}

/** In tab order. The first is the page an admin lands on: what waits for them. */
export const PLATFORM_PAGES: readonly { label: string; href: string }[] = [
  { label: "Overview", href: "/platform/" },
  { label: "Apps", href: "/platform/apps/" },
  { label: "Access", href: "/platform/access/" },
  { label: "Egress", href: "/platform/egress/" },
  { label: "Settings", href: "/platform/settings/" },
];

/**
 * The tabs of the platform area. Overview carries the number of requests that
 * wait for an answer (`null` while the request list has not loaded), so an
 * admin on any tab sees that the inbox is not empty.
 */
export function platformNavItems(
  pendingRequests: number | null,
): PlatformNavItem[] {
  return PLATFORM_PAGES.map((page) => ({
    ...page,
    badge:
      page.href === "/platform/" &&
      pendingRequests !== null &&
      pendingRequests > 0
        ? {
            text: String(pendingRequests),
            description: `${pluralize(pendingRequests, "request")} waiting`,
          }
        : null,
  }));
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
