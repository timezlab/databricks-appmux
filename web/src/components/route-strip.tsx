import { cn } from "cn";

export interface RouteStripProps {
  /** The app's slug; the strip shows its route, `/<slug>/`. */
  slug: string;
  /**
   * Makes the strip a link that opens the app: pass `appLink(slug)`. It is a
   * plain `<a>`, like `AppLink`, because the app's route is outside the admin
   * UI's base path; a page of the admin UI needs `next/link` instead.
   */
  href?: string;
}

const STRIP_CLASSES =
  "inline-block rounded-strip border border-strip-border bg-strip px-1.5 font-mono text-xs leading-5 font-medium whitespace-nowrap text-strip-foreground no-underline";

/**
 * An app's route on a paper-coloured strip with square corners, in the
 * monospace face (docs/product/admin-ui-design.md, "The route strip"). It is
 * the one place the interface is not neutral, so a person finds their app by
 * it wherever the app is named.
 */
export function RouteStrip({ slug, href }: RouteStripProps) {
  const route = `/${slug}/`;
  if (href === undefined) return <span className={STRIP_CLASSES}>{route}</span>;
  return (
    <a
      className={cn(STRIP_CLASSES, "hover:border-strip-foreground")}
      href={href}
    >
      {route}
    </a>
  );
}
