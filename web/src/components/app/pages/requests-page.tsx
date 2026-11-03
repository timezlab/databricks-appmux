"use client";

import { useAppPageContext } from "../app-context";
import { RequestsSection } from "../requests-section";

/** `/app/requests/`: the app's settings and delegation requests to a platform admin. */
export function AppRequestsPage() {
  const { app, actor, actions, requests, reloadRequests, reloadAll } =
    useAppPageContext();
  return (
    <RequestsSection
      app={app}
      actor={actor}
      actions={actions}
      requests={requests}
      onReloadRequests={reloadRequests}
      onReload={reloadAll}
    />
  );
}
