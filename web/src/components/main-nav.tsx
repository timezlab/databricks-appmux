"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { NAV_ITEMS, isCurrentNavEntry } from "./nav-items";

/**
 * The top navigation. The current page carries `aria-current` and the
 * `primary` bar of the shared `bar-item` geometry (globals.css), the same as
 * the tabs and the sidebar.
 */
export function MainNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main">
      <ul className="-mb-px flex flex-wrap gap-x-1">
        {NAV_ITEMS.map((item) => (
          <li key={item.href}>
            <Link
              className="bar-item px-3 py-3 text-sm"
              href={item.href}
              aria-current={
                isCurrentNavEntry(pathname, item) ? "page" : undefined
              }
            >
              {item.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
