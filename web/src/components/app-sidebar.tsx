"use client";

import { cn } from "cn";
import type { LucideIcon } from "lucide-react";
import {
  ChevronLeftIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
} from "lucide-react";
import Link from "next/link";
import { useId } from "react";

import { useStoredFlag } from "@/hooks/use-stored-flag";
import { Button as UiButton } from "./ui/button";

/** The `localStorage` key of the collapsed state, shared by every sidebar. */
export const SIDEBAR_COLLAPSED_KEY = "appmux.sidebar.collapsed";

export interface SidebarItemData {
  /** A literal path or the output of `appPagePath`; `next/link` adds the base path. */
  href: string;
  label: string;
  icon: LucideIcon;
  /** Something to do there (requests waiting), shown beside the label. Leave it out when it means nothing. */
  count?: number;
}

export interface SidebarItemProps extends SidebarItemData {
  isCurrent: boolean;
  /** Icons only: the label stays for screen readers and in the hover title. */
  isCollapsed: boolean;
}

/** One entry of the sidebar, on the shared `bar-item` geometry (a bar on its start edge). */
export function SidebarItem({
  href,
  label,
  icon: Icon,
  count,
  isCurrent,
  isCollapsed,
}: SidebarItemProps) {
  return (
    <Link
      className="bar-item w-full px-3 py-2 text-sm"
      href={href}
      aria-current={isCurrent ? "page" : undefined}
      title={isCollapsed ? label : undefined}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span
        className={cn("min-w-0 flex-1 truncate", isCollapsed && "md:sr-only")}
      >
        {label}
      </span>
      {count === undefined ? null : (
        <span className="rounded-full bg-primary px-1.5 text-xs leading-5 text-primary-foreground tabular-nums">
          {count}
        </span>
      )}
    </Link>
  );
}

export interface AppSidebarProps {
  /** Names the landmark ("App sections"). */
  label: string;
  items: readonly SidebarItemData[];
  /** The `href` of the item for the page on screen. */
  current: string;
  /** Where "‹ All apps" goes. */
  backHref: string;
  backLabel: string;
}

/**
 * The left column inside one app and inside Platform
 * (docs/product/admin-ui-design.md, Layout): a link back to the list, one item
 * per section, and a button that collapses it to icons (kept in this browser).
 * On a narrow screen the column stacks above the content; the menu that will
 * replace it there is not built yet.
 */
export function AppSidebar({
  label,
  items,
  current,
  backHref,
  backLabel,
}: AppSidebarProps) {
  const [isCollapsed, setIsCollapsed] = useStoredFlag(SIDEBAR_COLLAPSED_KEY);
  const listId = useId();
  return (
    <nav
      aria-label={label}
      className={cn(
        "flex shrink-0 flex-col gap-2 md:self-start",
        isCollapsed ? "md:w-14" : "md:w-56",
      )}
    >
      <div className="flex items-center justify-between gap-1">
        <Link
          className="inline-flex items-center gap-1 px-2 py-1 text-sm text-muted-foreground no-underline hover:text-foreground"
          href={backHref}
          title={isCollapsed ? backLabel : undefined}
        >
          <ChevronLeftIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className={cn(isCollapsed && "md:sr-only")}>{backLabel}</span>
        </Link>
        <UiButton
          type="button"
          variant="ghost"
          size="icon-sm"
          className="hidden md:inline-flex"
          aria-controls={listId}
          aria-expanded={!isCollapsed}
          aria-label={
            isCollapsed ? "Expand the sidebar" : "Collapse the sidebar"
          }
          title={isCollapsed ? "Expand the sidebar" : "Collapse the sidebar"}
          onClick={() => {
            setIsCollapsed(!isCollapsed);
          }}
        >
          {isCollapsed ? (
            <PanelLeftOpenIcon aria-hidden="true" />
          ) : (
            <PanelLeftCloseIcon aria-hidden="true" />
          )}
        </UiButton>
      </div>
      <ul id={listId} className="bar-axis-vertical flex flex-col">
        {items.map((item) => (
          <li key={item.href}>
            <SidebarItem
              {...item}
              isCurrent={item.href === current}
              isCollapsed={isCollapsed}
            />
          </li>
        ))}
      </ul>
    </nav>
  );
}
