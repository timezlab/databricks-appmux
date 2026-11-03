"use client";

import { useAppPageContext } from "../app-context";
import { ReleaseSection } from "../release-section";

/** `/app/deployments/`: the app's releases and the source viewer. */
export function AppDeploymentsPage() {
  const { app, actor, actions, reloadAll } = useAppPageContext();
  return (
    <ReleaseSection
      app={app}
      actor={actor}
      actions={actions}
      onReload={reloadAll}
    />
  );
}
