"use client";

import { useState } from "react";
import type { ReactNode } from "react";

import { getApp, getMe, listApps, listRequests } from "@/lib/api";
import { resolveActor } from "@/lib/app/actions";
import { appNavItems } from "@/lib/app/sections";
import { appLoadView, pollData } from "@/lib/app/view";
import type { WriteNotice } from "@/lib/app/writes";
import { usePoll } from "@/hooks/use-poll";
import { ErrorNotice } from "@/components/error-notice";
import { LiveRegion } from "@/components/live-region";
import { Loading } from "@/components/loading";
import { Notice } from "@/components/notice";
import { PageHeader } from "@/components/page-header";
import { SubNav } from "@/components/sub-nav";
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
  /** The page of the app being shown (status, releases, ...): it reads the app from the context. */
  children: ReactNode;
}

/**
 * The shell of one app's pages. Four polls feed it: the app (needs `CAN_MANAGE`), who
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

  const [notice, setNotice] = useState<WriteNotice | null>(null);

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
        <SubNav
          label={`${view.app.display_name} sections`}
          items={appNavItems(view.app, pollData(requests)?.items ?? null)}
        />
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
      </div>
    </AppPageProvider>
  );
}
