"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { isCurrentSubNavItem } from "./nav-items";

export interface SubNavItem {
  label: string;
  /** A literal path or the output of `appPagePath`; `next/link` adds the base path. */
  href: string;
  /** A count worth seeing before the tab is opened ("3" waiting). Say what it counts in `description`. */
  badge?: { text: string; description: string } | null;
}

export interface SubNavProps {
  /** Names the landmark ("Platform sections"). */
  label: string;
  items: readonly SubNavItem[];
}

/**
 * The tabs of a section whose pages are routes of their own (the platform's,
 * one app's). They are links, not ARIA tabs: each opens a page of the export.
 * The current one carries `aria-current`, is bold and has the brand-red bar,
 * so it does not depend on hue. The row wraps rather than scrolls, so at phone
 * width every tab is visible without a hidden scroll area.
 */
export function SubNav({ label, items }: SubNavProps) {
  const pathname = usePathname();
  return (
    <nav aria-label={label} className="border-b">
      <ul className="-mb-px flex flex-wrap gap-x-1">
        {items.map((item) => (
          <li key={item.label}>
            <Link
              className="inline-flex items-center gap-2 border-b-[3px] border-transparent px-3 py-2.5 text-muted-foreground no-underline hover:text-foreground hover:underline aria-[current=page]:border-brand aria-[current=page]:font-bold aria-[current=page]:text-foreground aria-[current=page]:hover:no-underline forced-colors:aria-[current=page]:border-[color:Highlight]"
              href={item.href}
              aria-current={
                isCurrentSubNavItem(pathname, item.href) ? "page" : undefined
              }
            >
              {item.label}
              {item.badge === undefined || item.badge === null ? null : (
                <>
                  <span
                    aria-hidden="true"
                    className="rounded-full bg-primary px-2 text-xs font-bold text-primary-foreground"
                  >
                    {item.badge.text}
                  </span>
                  <span className="sr-only">, {item.badge.description}</span>
                </>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
