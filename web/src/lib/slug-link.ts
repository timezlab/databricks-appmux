/**
 * The link to a child app (design 7.2): `/<slug>/` on the same origin.
 *
 * It is NOT under the admin UI's base path (`/_appmux`), so it must be written
 * as a plain `<a href>`; `next/link` would prefix the base path. A slug reaches
 * an `href` only through `appLink`, which checks it against the slug pattern of
 * the contract first: the pattern allows no `/`, `.`, `%`, `:` or `\`, so a
 * value such as `../admin`, `//evil.example` or `javascript:alert(1)` can never
 * become a link, and `_appmux`, the reserved prefix, is not a slug.
 */
import { SLUG_PATTERN } from "@/lib/api/schemas/primitives";

export function isValidSlug(value: unknown): value is string {
  return typeof value === "string" && SLUG_PATTERN.test(value);
}

/** `/<slug>/`, or `null` when `slug` is not a valid slug (show it as text). */
export function appLink(slug: string): string | null {
  return isValidSlug(slug) ? `/${slug}/` : null;
}
