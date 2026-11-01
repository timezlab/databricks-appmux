import type { ReactNode } from "react";

export interface PageHeaderProps {
  /** The page's one `<h1>`. */
  title: string;
  /** One sentence under the title. */
  description?: ReactNode;
  /** Buttons for the whole page (Refresh, Register app), at the right on a wide screen. */
  actions?: ReactNode;
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between sm:gap-x-6">
      <div className="flex min-w-0 flex-col gap-1">
        <h1>{title}</h1>
        {description === undefined ? null : (
          <div className="text-muted-foreground">{description}</div>
        )}
      </div>
      {actions === undefined ? null : (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
  );
}
