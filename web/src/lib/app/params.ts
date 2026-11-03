/**
 * Which app the app page shows. A static export cannot prerender a page per
 * slug (the slugs are not known at build time), so there is one page,
 * `/app/?slug=sales`, and the slug is read from the query string in the
 * browser. The query string is input from outside: it is checked against the
 * slug pattern of the contract before it reaches a request path, and a value
 * that fails is only ever shown as text.
 */
import { isValidSlug } from "@/lib/slug-link";

export const APP_SLUG_PARAMETER = "slug";

/** How much of a rejected value is echoed back; the rest is cut, so a long query cannot flood the page. */
const MAX_SHOWN_INVALID_CHARS = 64;

export type AppParam =
  | { status: "valid"; slug: string }
  | { status: "missing" }
  | { status: "invalid"; value: string };

/** `searchParams` is what `useSearchParams()` returns, or any `URLSearchParams`. */
export function readAppParam(searchParams: {
  get: (name: string) => string | null;
}): AppParam {
  const value = searchParams.get(APP_SLUG_PARAMETER);
  if (value === null || value === "") return { status: "missing" };
  // `isValidSlug` is a type guard, so after its `return` the compiler would
  // narrow `value` to `never`; `shownText` keeps the plain string for the rest.
  const shownText: string = value;
  if (isValidSlug(value)) return { status: "valid", slug: value };
  return {
    status: "invalid",
    value: shownText.slice(0, MAX_SHOWN_INVALID_CHARS),
  };
}

/**
 * The sections of one app (docs/product/admin-ui-design.md, "The app's
 * sections"), in sidebar order, each its own route of the export (they share
 * the `?slug=` mechanism above, so none needs a segment unknown at build
 * time). The first is the app's front door: what state it is in and what to
 * do next. The routes of the earlier five pages (`releases`, `sharing`) are
 * gone, not redirected: a static export has no redirects.
 */
export const APP_SECTIONS = [
  "overview",
  "authorization",
  "deployments",
  "logs",
  "security",
  "environment",
  "resources",
  "insights",
  "requests",
  "settings",
] as const;

export type AppSection = (typeof APP_SECTIONS)[number];

const APP_SECTION_PATH: Readonly<Record<AppSection, string>> = {
  overview: "/app/",
  authorization: "/app/authorization/",
  deployments: "/app/deployments/",
  logs: "/app/logs/",
  security: "/app/security/",
  environment: "/app/environment/",
  resources: "/app/resources/",
  insights: "/app/insights/",
  requests: "/app/requests/",
  settings: "/app/settings/",
};

/**
 * The path to an app's page (its overview unless `section` says another),
 * for `next/link` (which adds the base path), or `null` when `slug` is not a
 * slug. The only way another page should link here.
 */
export function appPagePath(
  slug: string,
  section: AppSection = "overview",
): string | null {
  return isValidSlug(slug)
    ? `${APP_SECTION_PATH[section]}?${APP_SLUG_PARAMETER}=${slug}`
    : null;
}

function withoutTrailingSlash(path: string): string {
  return path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
}

/**
 * The section whose page `pathname` is (from `usePathname()`, which has
 * already removed the base path; the export's trailing slash may or may not be
 * there), or `null` for any other path.
 */
export function appSectionAt(pathname: string | null): AppSection | null {
  if (pathname === null || pathname === "") return null;
  const path = withoutTrailingSlash(pathname);
  return (
    APP_SECTIONS.find(
      (section) => withoutTrailingSlash(APP_SECTION_PATH[section]) === path,
    ) ?? null
  );
}
