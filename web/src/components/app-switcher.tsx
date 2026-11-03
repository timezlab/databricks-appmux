"use client";

import { cn } from "cn";
import { CheckIcon, ChevronDownIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import {
  rememberAppDisplayNames,
  useAppDisplayName,
} from "@/hooks/use-app-display-name";
import { usePoll } from "@/hooks/use-poll";
import { listApps } from "@/lib/api/launcher";
import type { AppList } from "@/lib/api";
import { appPagePath, appSectionAt } from "@/lib/app/params";
import { appStatePresentation } from "@/lib/status-presentation";
import { EmptyState } from "./empty-state";
import { ErrorNotice } from "./error-notice";
import { Lamp, lampStateOfTone } from "./lamp";
import { RouteStrip } from "./route-strip";
import { SkeletonLines } from "./skeletons";
import { Button as UiButton } from "./ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "./ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";

/** How often the open list refreshes; closed, it is not fetched at all. */
const OPEN_LIST_POLL_INTERVAL_MS = 30_000;

/**
 * The page of `targetSlug` that matches the one on screen: from an app's
 * Deployments, the other app's Deployments. A page that is no section leads to
 * the other app's overview.
 */
function samePageOf(
  pathname: string | null,
  targetSlug: string,
): string | null {
  return appPagePath(targetSlug, appSectionAt(pathname) ?? undefined);
}

function AppRows({
  apps,
  currentSlug,
  onChoose,
}: {
  apps: AppList["items"];
  currentSlug: string;
  onChoose: (slug: string) => void;
}) {
  return (
    <Command label="Apps" defaultValue={currentSlug}>
      <CommandInput placeholder="Search apps" autoFocus />
      <CommandList>
        <CommandEmpty>No app matches.</CommandEmpty>
        <CommandGroup>
          {apps.map((app) => {
            const state = appStatePresentation(app.state);
            return (
              <CommandItem
                key={app.slug}
                value={app.slug}
                keywords={[app.display_name]}
                onSelect={() => {
                  onChoose(app.slug);
                }}
              >
                <span className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
                  <span className="max-w-full truncate font-medium">
                    {app.display_name}
                    {app.slug === currentSlug ? (
                      <span className="sr-only"> (current)</span>
                    ) : null}
                  </span>
                  <RouteStrip slug={app.slug} />
                </span>
                <Lamp
                  state={lampStateOfTone(state.tone)}
                  label={state.label}
                  description={state.description}
                />
                {/* Combobox's mark of the chosen row: once the highlight moves
                    off the current app, this still shows which one is on screen. */}
                <CheckIcon
                  className={cn(
                    app.slug === currentSlug ? undefined : "invisible",
                  )}
                  aria-hidden="true"
                />
              </CommandItem>
            );
          })}
        </CommandGroup>
      </CommandList>
    </Command>
  );
}

/**
 * The `/ <app> ▾` of the top bar: the app on screen, and a searchable list of
 * the others (docs/product/admin-ui-design.md, Layout). The list is read when
 * the popover opens, not with the page, and choosing an app goes to the same
 * section of it. The name shows once this page has read it (here or on the app
 * page, through `rememberAppDisplayNames`); until then, the app's route strip.
 */
export function AppSwitcher({ slug }: { slug: string }) {
  const [isOpen, setIsOpen] = useState(false);
  const displayName = useAppDisplayName(slug);
  const pathname = usePathname();
  const router = useRouter();
  const apps = usePoll(
    async (signal) => {
      const list = await listApps({ signal });
      rememberAppDisplayNames(list.items);
      return list;
    },
    { enabled: isOpen, intervalMs: OPEN_LIST_POLL_INTERVAL_MS },
  );

  function choose(targetSlug: string) {
    setIsOpen(false);
    if (targetSlug === slug) return;
    const target = samePageOf(pathname, targetSlug);
    if (target !== null) router.push(target);
  }

  let body;
  switch (apps.status) {
    case "loading":
      body = (
        <div className="flex flex-col gap-2 p-3">
          <p role="status" className="sr-only">
            Loading the apps…
          </p>
          <SkeletonLines lines={3} />
        </div>
      );
      break;
    case "failed":
      body = (
        <div className="p-2">
          <ErrorNotice
            error={apps.error}
            context="Could not load the apps"
            onRetry={apps.refresh}
          />
        </div>
      );
      break;
    case "ready":
    case "stale":
      body =
        apps.data.items.length === 0 ? (
          <EmptyState
            title="No apps yet"
            action={
              <Link
                href="/"
                onClick={() => {
                  setIsOpen(false);
                }}
              >
                Go to the launcher
              </Link>
            }
          >
            Register one from the launcher.
          </EmptyState>
        ) : (
          <AppRows
            apps={apps.data.items}
            currentSlug={slug}
            onChoose={choose}
          />
        );
      break;
  }

  return (
    <Popover open={isOpen} onOpenChange={setIsOpen}>
      <PopoverTrigger asChild>
        <UiButton
          type="button"
          variant="ghost"
          className="h-10 min-w-0 shrink gap-1.5 px-2 text-foreground"
          // The name starts with what is on screen (WCAG 2.5.3, label in name).
          aria-label={`App: ${displayName ?? `/${slug}/`}`}
          title="Switch app"
        >
          {displayName === undefined ? (
            <RouteStrip slug={slug} />
          ) : (
            <span className="truncate">{displayName}</span>
          )}
          <ChevronDownIcon
            className="text-muted-foreground"
            aria-hidden="true"
          />
        </UiButton>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        aria-label="Switch app"
        className="w-80 max-w-[calc(100vw-2rem)] p-0"
      >
        {body}
      </PopoverContent>
    </Popover>
  );
}
