"use client";

import Link from "next/link";
import { useState } from "react";
import type { ReactNode } from "react";

import { getMe, getPlatform, listRequests } from "@/lib/api";
import type { AdminViewer } from "@/lib/platform/admins";
import { platformAccess } from "@/lib/platform/access";
import { platformNavItems } from "@/lib/platform/sections";
import { pollData } from "@/lib/poll/data";
import { inboxRequests } from "@/lib/request-views";
import { usePoll } from "@/hooks/use-poll";
import { Button } from "@/components/button";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { LiveRegion } from "@/components/live-region";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { RelativeTime } from "@/components/relative-time";
import { SubNav } from "@/components/sub-nav";
import { PlatformDataProvider, PlatformProvider } from "./platform-context";

/** Who may open this page changes rarely; `GET /me` is asked again this often. */
const ME_POLL_INTERVAL_MS = 30_000;

const PAGE_TITLE = "Platform";

/**
 * The shell of the platform pages (design 10.1, 9.4), the layout of
 * `/platform/*`: access check, the one `GET /platform` poll, the request list,
 * the title and the tab bar. The pages (overview, apps, access, egress,
 * settings) are its children and read what it fetched from a context, so a
 * move between tabs keeps the data, the polling and any message on screen.
 * Nothing about the platform is requested until `GET /me` says the visitor is
 * a platform admin; the server checks the same on every call.
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
        <SubNav
          label="Platform sections"
          items={platformNavItems(
            requestList === null
              ? null
              : inboxRequests(requestList.items).length,
          )}
        />
        <LiveRegion message={announcement} />
        {platform.status === "loading" ? (
          <Loading label="Loading the platform" />
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
      </div>
    </PlatformProvider>
  );
}
