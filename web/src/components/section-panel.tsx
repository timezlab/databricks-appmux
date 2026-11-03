import type { ReactNode } from "react";

export interface SectionPanelProps {
  children: ReactNode;
}

/**
 * The one inset panel a page's content sits in (docs/product/admin-ui-design.md,
 * Layout): the card colour on the page colour, a 12 px radius, a hairline
 * border, 1120 px at most and left aligned. Inside it sections are ruled, not
 * boxed into further cards.
 */
export function SectionPanel({ children }: SectionPanelProps) {
  return (
    <div className="flex w-full max-w-280 flex-col gap-6 rounded-surface border bg-card p-4 text-card-foreground sm:p-6">
      {children}
    </div>
  );
}
