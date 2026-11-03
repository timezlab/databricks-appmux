"use client";

import { useAppPageContext } from "../app-context";
import { SharingSection } from "../sharing-section";

/** `/app/authorization/`: who may use or manage the app. */
export function AppAuthorizationPage() {
  const { app, actor, actions, reloadAll } = useAppPageContext();
  return (
    <SharingSection
      app={app}
      actor={actor}
      actions={actions}
      onReload={reloadAll}
    />
  );
}
