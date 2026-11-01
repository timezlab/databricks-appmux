"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { NAV_ITEMS, isCurrentNavEntry } from "./nav-items";

/** The top navigation, in the dark header band. The current page is marked with `aria-current`, shown bold and carries the brand-red bar. */
export function MainNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main">
      <ul className="flex flex-wrap gap-x-1">
        {NAV_ITEMS.map((item) => (
          <li key={item.href}>
            <Link
              className="inline-block border-b-[3px] border-transparent px-3 py-3.5 text-chrome-muted no-underline hover:text-chrome-foreground hover:underline aria-[current=page]:border-brand aria-[current=page]:font-bold aria-[current=page]:text-chrome-foreground aria-[current=page]:hover:no-underline forced-colors:aria-[current=page]:border-[color:Highlight]"
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
