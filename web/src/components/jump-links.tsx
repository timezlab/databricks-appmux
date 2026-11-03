export interface JumpLink {
  label: string;
  /** An anchor on the same page, written as a literal (`#traffic`). */
  href: string;
}

/**
 * Links to the parts of a long page, so a page of many parts is entered by its
 * part, not scrolled. One wrapping row above the content at every width: the
 * page already sits beside a sidebar, which leaves no room for a third column.
 * Plain anchors: no script, nothing to hash.
 */
export function JumpLinks({
  label,
  items,
}: {
  label: string;
  items: readonly JumpLink[];
}) {
  return (
    <nav aria-label={label}>
      <ul className="flex flex-wrap gap-x-4 gap-y-1">
        {items.map((item) => (
          <li key={item.href}>
            <a className="inline-block py-1" href={item.href}>
              {item.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
