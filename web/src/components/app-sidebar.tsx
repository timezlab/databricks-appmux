"use client";

import { cn } from "cn";
import type { LucideIcon } from "lucide-react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
} from "lucide-react";
import Link from "next/link";
import { useId, useState } from "react";

import { useStoredFlag } from "@/hooks/use-stored-flag";
import { Button as UiButton } from "./ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";

/** The `localStorage` key of the collapsed state, shared by every sidebar. */
export const SIDEBAR_COLLAPSED_KEY = "appmux.sidebar.collapsed";

export interface SidebarItemData {
  /** A literal path or the output of `appPagePath`; `next/link` adds the base path. */
  href: string;
  label: string;
  icon: LucideIcon;
  /** Something to do there (requests waiting), shown beside the label. Leave it out when it means nothing. */
  count?: number;
  /** What the count means, in words ("2 things need attention"): read in place of the bare number, and the hover title. */
  countDescription?: string;
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
  countDescription,
  isCurrent,
  isCollapsed,
}: SidebarItemProps) {
  const hasCountDescription =
    count !== undefined && countDescription !== undefined;
  const collapsedTitle = hasCountDescription
    ? `${label}, ${countDescription}`
    : label;
  return (
    <Link
      // At least 40 px tall on a phone, where it is a row of the section menu.
      className="bar-item w-full max-md:min-h-10"
      href={href}
      aria-current={isCurrent ? "page" : undefined}
      title={isCollapsed ? collapsedTitle : undefined}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span
        className={cn("min-w-0 flex-1 truncate", isCollapsed && "md:sr-only")}
      >
        {label}
      </span>
      {count === undefined ? null : (
        // With a description, a screen reader reads the words, not the bare number.
        <span
          aria-hidden={hasCountDescription ? "true" : undefined}
          className="rounded-full bg-primary px-1.5 text-xs leading-5 text-primary-foreground tabular-nums"
          title={countDescription}
        >
          {count}
        </span>
      )}
      {hasCountDescription ? (
        <span className="sr-only">, {countDescription}</span>
      ) : null}
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
 * Below 768 px: one button, "Section: <current>", that opens the same items
 * and the back link in a non-modal popover (a modal one would lock scrolling
 * with an injected `<style>`, which the CSP refuses). Choosing an item, Escape
 * and a click outside close it.
 */
function PhoneSectionMenu({
  label,
  items,
  current,
  backHref,
  backLabel,
}: AppSidebarProps) {
  const [isOpen, setIsOpen] = useState(false);
  const currentItem = items.find((item) => item.href === current);
  const CurrentIcon = currentItem?.icon;
  const close = () => {
    setIsOpen(false);
  };
  return (
    <Popover open={isOpen} onOpenChange={setIsOpen}>
      <PopoverTrigger asChild>
        <UiButton
          type="button"
          variant="outline"
          className="h-10 w-full justify-between md:hidden"
          aria-label={
            currentItem === undefined ? label : `Section: ${currentItem.label}`
          }
        >
          <span className="flex min-w-0 items-center gap-2">
            {CurrentIcon === undefined ? null : (
              <CurrentIcon aria-hidden="true" />
            )}
            {currentItem === undefined ? (
              <span className="truncate">{label}</span>
            ) : (
              <span className="truncate">
                <span className="text-muted-foreground">Section: </span>
                {currentItem.label}
              </span>
            )}
          </span>
          <ChevronDownIcon
            className="text-muted-foreground"
            aria-hidden="true"
          />
        </UiButton>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        aria-label={label}
        className="flex w-(--radix-popover-trigger-width) flex-col gap-1 p-1"
      >
        <Link
          className="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-muted-foreground no-underline hover:text-foreground"
          href={backHref}
          onClick={close}
        >
          <ChevronLeftIcon className="size-4 shrink-0" aria-hidden="true" />
          <span>{backLabel}</span>
        </Link>
        <ul className="bar-axis-vertical flex flex-col">
          {items.map((item) => (
            // The click of the link inside closes the menu as it navigates.
            <li key={item.href} onClick={close}>
              <SidebarItem
                {...item}
                isCurrent={item.href === current}
                isCollapsed={false}
              />
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

/**
 * The left column inside one app and inside Platform
 * (docs/product/admin-ui-design.md, Layout): a link back to the list, one item
 * per section, and a button that collapses it to icons (kept in this browser).
 * Below 768 px the column gives way to one menu button (`PhoneSectionMenu`).
 */
export function AppSidebar(props: AppSidebarProps) {
  const { label, items, current, backHref, backLabel } = props;
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
      <PhoneSectionMenu {...props} />
      <div className="hidden items-center justify-between gap-1 md:flex">
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
      <ul id={listId} className="bar-axis-vertical hidden flex-col md:flex">
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
