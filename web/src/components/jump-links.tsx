export interface JumpLink {
  label: string;
  /** An anchor on the same page, written as a literal (`#traffic`). */
  href: string;
}

/**
 * Links to the parts of a long page. They stay in view beside the content on a
 * wide screen and sit above it on a narrow one, so a page of many parts is
 * entered by its part, not scrolled. Plain anchors: no script, nothing to hash.
 */
export function JumpLinks({
  label,
  items,
}: {
  label: string;
  items: readonly JumpLink[];
}) {
  return (
    <nav aria-label={label} className="lg:sticky lg:top-4 lg:self-start">
      <ul className="flex flex-wrap gap-x-4 gap-y-1 lg:flex-col lg:gap-y-0.5 lg:border-l-2 lg:pl-3">
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
