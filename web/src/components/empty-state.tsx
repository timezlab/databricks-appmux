import type { ReactNode } from "react";

import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "./ui/empty";

export interface EmptyStateProps {
  /** What is missing ("No apps yet"). */
  title: string;
  /** Why, or what to do about it. */
  children?: ReactNode;
  /** The one next step, usually a `Button` or `AppLink`. */
  action?: ReactNode;
}

/** The placeholder for a list or section with nothing in it, so it is never a blank gap. */
export function EmptyState({ title, children, action }: EmptyStateProps) {
  return (
    <Empty>
      <EmptyHeader>
        <EmptyTitle>{title}</EmptyTitle>
        {children === undefined ? null : (
          <EmptyDescription>{children}</EmptyDescription>
        )}
      </EmptyHeader>
      {action === undefined ? null : <EmptyContent>{action}</EmptyContent>}
    </Empty>
  );
}
