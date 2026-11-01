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
 * The pages of one app, each its own route of the export (they share the
 * `?slug=` mechanism above, so none needs a segment unknown at build time).
 * The first is the app's front door: what state it is in and what to do next.
 */
export const APP_SECTIONS = [
  "status",
  "releases",
  "resources",
  "sharing",
  "requests",
] as const;

export type AppSection = (typeof APP_SECTIONS)[number];

const APP_SECTION_PATH: Readonly<Record<AppSection, string>> = {
  status: "/app/",
  releases: "/app/releases/",
  resources: "/app/resources/",
  sharing: "/app/sharing/",
  requests: "/app/requests/",
};

/**
 * The path to an app's page (its status page unless `section` says another),
 * for `next/link` (which adds the base path), or `null` when `slug` is not a
 * slug. The only way another page should link here.
 */
export function appPagePath(
  slug: string,
  section: AppSection = "status",
): string | null {
  return isValidSlug(slug)
    ? `${APP_SECTION_PATH[section]}?${APP_SLUG_PARAMETER}=${slug}`
    : null;
}
