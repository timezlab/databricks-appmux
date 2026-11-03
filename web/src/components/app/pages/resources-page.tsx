"use client";

import { useAppPageContext } from "../app-context";
import { ResourceArea } from "../resource-area";

/** `/app/resources/`: what the app needs and what is attached to it. */
export function AppResourcesPage() {
  const { app, actor, actions, reloadAll } = useAppPageContext();
  return (
    <ResourceArea
      app={app}
      actor={actor}
      actions={actions}
      onReload={reloadAll}
    />
  );
}
