"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { readAppParam } from "@/lib/app/params";
import { AppSwitcher } from "./app-switcher";
import { MainNav } from "./main-nav";
import { isCurrentNavItem } from "./nav-items";
import { ThemeToggle } from "./theme-toggle";

/** `/ <app> ▾` for the app named by `?slug=`, or nothing when there is no valid slug. */
function AppBreadcrumb() {
  const param = readAppParam(useSearchParams());
  if (param.status !== "valid") return null;
  return (
    <div className="flex min-w-0 items-center gap-1">
      <span aria-hidden="true" className="text-xl text-muted-foreground">
        /
      </span>
      <AppSwitcher slug={param.slug} />
    </div>
  );
}

/**
 * The bar above every page (docs/product/admin-ui-design.md, Layout): the page
 * colour with one hairline under it. The first line holds the wordmark (plain
 * text, there is no logo), on the pages of one app the breadcrumb `/ <app> ▾`,
 * and the theme switch at the end; the main navigation is the second line.
 *
 * The breadcrumb reads `?slug=`, which a static export knows only in the
 * browser, so it waits in `Suspense` and the prerendered bar never depends on
 * it. The rest reads only the path, which each prerendered page knows.
 */
export function TopBar() {
  const isAppPage = isCurrentNavItem(usePathname(), "/app/");
  return (
    <header className="border-b bg-background">
      <div className="mx-auto flex max-w-6xl flex-col px-4">
        <div className="flex min-w-0 items-center gap-1 pt-2">
          <Link
            className="flex shrink-0 items-baseline gap-2 py-1 pr-1 text-xl font-semibold tracking-heading text-foreground no-underline hover:no-underline"
            href="/"
          >
            <span>appmux</span>
            {isAppPage ? null : (
              <span className="hidden text-xs font-normal tracking-normal text-muted-foreground sm:inline">
                for Databricks Apps
              </span>
            )}
          </Link>
          {isAppPage ? (
            <Suspense fallback={null}>
              <AppBreadcrumb />
            </Suspense>
          ) : null}
          <div className="ml-auto shrink-0">
            <ThemeToggle />
          </div>
        </div>
        <MainNav />
      </div>
    </header>
  );
}
