"use client";

import { useAppPageContext } from "../app-context";
import { AppOverview } from "../app-header";

/** `/app/`: the app at a glance, what needs attention, and the app-level buttons. */
export function AppOverviewPage() {
  const { app, actor, actions, reloadAll } = useAppPageContext();
  return (
    <AppOverview
      app={app}
      actor={actor}
      actions={actions}
      onReload={reloadAll}
    />
  );
}
