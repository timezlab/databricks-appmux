"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import type { ReactNode } from "react";

import { getMe, getPlatform, listRequests } from "@/lib/api";
import type { AdminViewer } from "@/lib/platform/admins";
import { platformAccess } from "@/lib/platform/access";
import { PLATFORM_PAGES, platformSidebarItems } from "@/lib/platform/sections";
import { pollData } from "@/lib/poll/data";
import { inboxRequests } from "@/lib/request-views";
import { usePoll } from "@/hooks/use-poll";
import { AppSidebar } from "@/components/app-sidebar";
import { Button } from "@/components/button";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { LiveRegion } from "@/components/live-region";
import { Loading } from "@/components/loading";
import { isCurrentSubNavItem } from "@/components/nav-items";
import { PageHeader } from "@/components/page-header";
import { RelativeTime } from "@/components/relative-time";
import { SectionPanel } from "@/components/section-panel";
import { SkeletonLines, SkeletonTable } from "@/components/skeletons";
import { PlatformDataProvider, PlatformProvider } from "./platform-context";

/** Who may open this page changes rarely; `GET /me` is asked again this often. */
const ME_POLL_INTERVAL_MS = 30_000;

const PAGE_TITLE = "Platform";

/** The areas whose first content is a table; the others open with text. */
const TABLE_FIRST_PAGES: ReadonlySet<string> = new Set([
  "/platform/apps/",
  "/platform/access/",
  "/platform/egress/",
]);

/**
 * What stands in for an area until `GET /platform` answers: its shape (a table
 * for the lists, lines for the inbox and the settings), so the panel does not
 * jump when the data lands. The status line says what loads.
 */
function PlatformSkeleton({ currentHref }: { currentHref: string }) {
  const isList = TABLE_FIRST_PAGES.has(currentHref);
  return (
    <div className="flex flex-col gap-4">
      <p className="sr-only" role="status">
        Loading the platform
      </p>
      <SkeletonLines lines={2} />
      {isList ? <SkeletonTable rows={3} columns={4} /> : <SkeletonLines />}
    </div>
  );
}

/**
 * The shell of the platform pages (design 10.1, 9.4), the layout of
 * `/platform/*`: access check, the one `GET /platform` poll, the request list,
 * the title, and the sidebar of the five areas beside the panel they open in.
 * The pages (overview, apps, access, egress, settings) are its children and
 * read what it fetched from a context, so a move between areas keeps the data,
 * the polling and any message on screen. Nothing about the platform is
 * requested until `GET /me` says the visitor is a platform admin; the server
 * checks the same on every call.
 */
export function PlatformPage({ children }: { children: ReactNode }) {
  const me = usePoll((signal) => getMe({ signal }), {
    intervalMs: ME_POLL_INTERVAL_MS,
  });
  const access = platformAccess(me);
  const isAllowed = access.state === "allowed";
  const platform = usePoll((signal) => getPlatform({ signal }), {
    enabled: isAllowed,
  });
  const requests = usePoll((signal) => listRequests({ signal }), {
    enabled: isAllowed,
  });
  // The sentence the live region reads out after a write.
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const pathname = usePathname();
  const currentHref =
    PLATFORM_PAGES.find((page) => isCurrentSubNavItem(pathname, page.href))
      ?.href ?? "";

  function refresh(): void {
    platform.refresh();
    requests.refresh();
  }

  switch (access.state) {
    case "checking":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title={PAGE_TITLE} />
          <Loading label="Checking your access" />
        </div>
      );
    case "error":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title={PAGE_TITLE} />
          <ErrorNotice
            error={access.error}
            context="Could not check your access"
            onRetry={me.refresh}
          />
        </div>
      );
    case "denied":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title={PAGE_TITLE} />
          <EmptyState
            title="This page is for platform admins"
            action={<Link href="/">Back to the launcher</Link>}
          >
            You are signed in as{" "}
            <span className="wrap-break-word">{access.email}</span>, who is not
            a platform admin. Ask a platform admin to add you.
          </EmptyState>
        </div>
      );
    case "allowed":
      break;
  }

  const viewer: AdminViewer = {
    email: access.email,
    groups:
      me.status === "ready" || me.status === "stale" ? me.data.groups : [],
  };

  const requestList = pollData(requests);

  return (
    <PlatformProvider value={{ refresh, announce: setAnnouncement }}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title={PAGE_TITLE}
          description={
            platform.status === "ready" || platform.status === "stale" ? (
              <>
                Shown as fetched <RelativeTime value={platform.updatedAtMs} />;
                it refreshes every few seconds.
              </>
            ) : (
              "Requests, apps, access, egress rules and settings of this appmux."
            )
          }
          actions={<Button onClick={refresh}>Refresh now</Button>}
        />
        <div className="flex flex-col gap-6 md:flex-row md:items-start">
          <AppSidebar
            label="Platform sections"
            items={platformSidebarItems(
              requestList === null
                ? null
                : inboxRequests(requestList.items).length,
            )}
            current={currentHref}
            backHref="/"
            backLabel="All apps"
          />
          {/* `min-w-0` lets a wide table shrink the panel instead of pushing it past the screen. */}
          <div className="min-w-0 flex-1">
            <SectionPanel>
              <LiveRegion message={announcement} />
              {platform.status === "loading" ? (
                <PlatformSkeleton currentHref={currentHref} />
              ) : null}
              {platform.status === "failed" ? (
                <ErrorNotice
                  error={platform.error}
                  context="Could not load the platform"
                  onRetry={refresh}
                />
              ) : null}
              {platform.status === "stale" ? (
                <ErrorNotice
                  error={platform.error}
                  context="Could not refresh the platform; showing the last data"
                  onRetry={refresh}
                />
              ) : null}
              {platform.status === "ready" || platform.status === "stale" ? (
                <PlatformDataProvider
                  value={{ view: platform.data, requests, viewer }}
                >
                  {children}
                </PlatformDataProvider>
              ) : null}
            </SectionPanel>
          </div>
        </div>
      </div>
    </PlatformProvider>
  );
}
