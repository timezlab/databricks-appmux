"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { getApp, getMe, listApps, listRequests } from "@/lib/api";
import { resolveActor } from "@/lib/app/actions";
import { appPagePath, appSectionAt } from "@/lib/app/params";
import { appNavItems } from "@/lib/app/sections";
import { appLoadView, pollData } from "@/lib/app/view";
import type { WriteNotice } from "@/lib/app/writes";
import { rememberAppDisplayNames } from "@/hooks/use-app-display-name";
import { usePoll } from "@/hooks/use-poll";
import { AppSidebar } from "@/components/app-sidebar";
import { ErrorNotice } from "@/components/error-notice";
import { LiveRegion } from "@/components/live-region";
import { Loading } from "@/components/loading";
import { Notice } from "@/components/notice";
import { PageHeader } from "@/components/page-header";
import { SectionPanel } from "@/components/section-panel";
import { AppPageProvider } from "./app-context";
import { AppTitle } from "./app-header";
import { AppProblem } from "./app-problem";
import { useAppActions } from "./use-app-actions";

/** The app moves while people watch it build and start; the poll shows it. */
const APP_POLL_INTERVAL_MS = 5_000;
/** Who the person is and what the launcher list says about them changes rarely. */
const DIRECTORY_POLL_INTERVAL_MS = 30_000;
const REQUESTS_POLL_INTERVAL_MS = 15_000;

export interface AppViewProps {
  slug: string;
  /** The section of the app being shown (overview, deployments, ...): it reads the app from the context. */
  children: ReactNode;
}

/**
 * The shell of one app's pages: the app's name on top, then the sidebar of its
 * sections beside the one panel the section sits in
 * (docs/product/admin-ui-design.md, Layout); below 768 px the sidebar stacks
 * above the panel. Four polls feed it: the app (needs `CAN_MANAGE`), who
 * the person is, the launcher list (their level on this app) and the requests.
 * What each section shows and decides is in `lib/app/*.ts`; this component
 * only wires them together. A write refreshes the app, the list and the
 * requests at once, so the page shows what the server now holds.
 *
 * Not on this page yet (design 7.3 lists them for the app page): the egress
 * rules (read-only), the health check, env entries, wheel sources and own-SP
 * keys, a release's build-log tail and detected warnings, and the app's
 * metrics. The page shows the delegation choices inside the request form only.
 * The T173 task text does not list them.
 */
export function AppView({ slug, children }: AppViewProps) {
  const app = usePoll((signal) => getApp(slug, { signal }), {
    intervalMs: APP_POLL_INTERVAL_MS,
    resetKey: `app:${slug}`,
  });
  const me = usePoll((signal) => getMe({ signal }), {
    intervalMs: DIRECTORY_POLL_INTERVAL_MS,
  });
  const apps = usePoll((signal) => listApps({ signal }), {
    intervalMs: DIRECTORY_POLL_INTERVAL_MS,
  });
  const view = appLoadView(app);
  const requests = usePoll((signal) => listRequests({ signal }), {
    intervalMs: REQUESTS_POLL_INTERVAL_MS,
    enabled: view.kind === "ready",
  });

  // The top bar's breadcrumb says "Sales dashboard" instead of the slug once
  // this page has read the name. An effect, not the render: handing the name
  // over wakes the top bar, which must not happen while this one renders.
  const readName = view.kind === "ready" ? view.app.display_name : null;
  useEffect(() => {
    if (readName !== null) {
      rememberAppDisplayNames([{ slug, display_name: readName }]);
    }
  }, [slug, readName]);

  const [notice, setNotice] = useState<WriteNotice | null>(null);
  const section = appSectionAt(usePathname());

  function refreshAll(): void {
    app.refresh();
    apps.refresh();
    requests.refresh();
  }

  const actions = useAppActions({
    onChanged: refreshAll,
    onNotice: setNotice,
  });
  const actor = resolveActor(pollData(me), pollData(apps)?.items ?? null, slug);
  // Without these two answers every button stays off, so say why.
  const actorFailure =
    me.status === "failed"
      ? me.error
      : apps.status === "failed"
        ? apps.error
        : null;

  switch (view.kind) {
    case "loading":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title={slug} />
          <Loading label="Loading the app" />
        </div>
      );
    case "not-found":
      return <AppProblem kind="not-found" slug={slug} />;
    case "no-access":
      return <AppProblem kind="no-access" slug={slug} />;
    case "failed":
      return (
        <div className="flex flex-col gap-4">
          <PageHeader title={slug} />
          <ErrorNotice
            error={view.error}
            context="Could not load the app"
            onRetry={app.refresh}
          />
        </div>
      );
    case "ready":
      break;
  }

  return (
    <AppPageProvider
      value={{
        app: view.app,
        actor,
        actions,
        requests,
        reloadRequests: requests.refresh,
        reloadAll: refreshAll,
      }}
    >
      <div className="flex flex-col gap-6">
        <AppTitle app={view.app} />
        <div className="flex flex-col gap-6 md:flex-row md:items-start">
          <AppSidebar
            label={`${view.app.display_name} sections`}
            items={appNavItems(view.app, pollData(requests)?.items ?? null)}
            current={
              (section === null ? null : appPagePath(slug, section)) ?? ""
            }
            backHref="/"
            backLabel="All apps"
          />
          {/* `min-w-0` lets a wide table shrink the panel instead of pushing it past the screen. */}
          <div className="min-w-0 flex-1">
            <SectionPanel>
              <LiveRegion message={notice?.text ?? null} />
              {notice === null ? null : (
                <Notice tone={notice.tone} title={notice.text} />
              )}
              {view.refreshError === null ? null : (
                <ErrorNotice
                  error={view.refreshError}
                  context="Could not refresh the app; showing the last copy"
                  onRetry={app.refresh}
                />
              )}
              {actorFailure === null ? null : (
                <ErrorNotice
                  error={actorFailure}
                  context="Could not check what you may do here"
                  onRetry={() => {
                    me.refresh();
                    apps.refresh();
                  }}
                />
              )}
              {children}
            </SectionPanel>
          </div>
        </div>
      </div>
    </AppPageProvider>
  );
}
