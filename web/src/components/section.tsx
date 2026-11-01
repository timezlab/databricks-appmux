"use client";

import { useId } from "react";
import type { ReactNode } from "react";

export interface SectionProps {
  /** The section's `<h2>`; it also names the region for screen readers. */
  title: string;
  /** Buttons for this section, beside the heading. */
  actions?: ReactNode;
  /** An anchor for a jump link (`#traffic`); the section stops a little below the top when it is jumped to. */
  id?: string;
  children: ReactNode;
}

export function Section({ title, actions, id, children }: SectionProps) {
  const headingId = useId();
  return (
    <section
      id={id}
      className="flex scroll-mt-4 flex-col gap-4"
      aria-labelledby={headingId}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 id={headingId}>{title}</h2>
        {actions === undefined ? null : (
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        )}
      </div>
      {children}
    </section>
  );
}
