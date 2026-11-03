/** The top navigation (design 10.1): one entry per screen that has its own page. */
export interface NavItem {
  label: string;
  /** Relative to the base path; `next/link` adds it. A page is a directory (`trailingSlash`). */
  href: string;
  /**
   * Other paths that belong to this screen, so its tab stays lit on them: the
   * pages of one app (`/app/...`) are part of the Apps screen, whose own page is
   * the root.
   */
  alsoCurrentUnder?: readonly string[];
}

// Every entry shows for everyone: the layout is static and cannot know the actor.
// A page that wants a link hidden decides that from `GET /me`.
export const NAV_ITEMS: readonly NavItem[] = [
  { label: "Apps", href: "/", alsoCurrentUnder: ["/app/"] },
  { label: "Platform", href: "/platform/" },
  { label: "Dashboard", href: "/dashboard/" },
];

function withoutTrailingSlash(path: string): string {
  return path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
}

/**
 * Whether `pathname` (from `usePathname()`, base path already removed) is the
 * page `href` names or lies under it. The root matches only itself.
 */
export function isCurrentNavItem(
  pathname: string | null,
  href: string,
): boolean {
  if (pathname === null || pathname === "") return false;
  const path = withoutTrailingSlash(pathname);
  const target = withoutTrailingSlash(href);
  if (target === "/") return path === "/";
  return path === target || path.startsWith(`${target}/`);
}

/** Whether the tab `item` is the current one: its own page, or a page listed in `alsoCurrentUnder`. */
export function isCurrentNavEntry(
  pathname: string | null,
  item: NavItem,
): boolean {
  return (
    isCurrentNavItem(pathname, item.href) ||
    (item.alsoCurrentUnder ?? []).some((href) =>
      isCurrentNavItem(pathname, href),
    )
  );
}

/**
 * Whether `pathname` is exactly the page `href` names, ignoring a trailing
 * slash and the query string. For the pages of a section (the platform page
 * picks its title with it): the first page, `/platform/`, must not stay
 * current on `/platform/apps/`, which `isCurrentNavItem` would do.
 */
export function isCurrentSubNavItem(
  pathname: string | null,
  href: string,
): boolean {
  if (pathname === null || pathname === "") return false;
  const target = href.split("?")[0] ?? href;
  return withoutTrailingSlash(pathname) === withoutTrailingSlash(target);
}
