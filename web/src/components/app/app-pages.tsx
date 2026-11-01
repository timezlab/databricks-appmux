"use client";

import { useAppPageContext } from "./app-context";
import { AppStatus } from "./app-header";
import { ReleaseSection } from "./release-section";
import { RequestsSection } from "./requests-section";
import { ResourceArea } from "./resource-area";
import { SharingSection } from "./sharing-section";

/*
 * The five pages of one app (`/app/`, `/app/releases/`, ...). Each reads the
 * app, the actor and the write queue from the shell (`AppView`) and puts one
 * area on screen; the sections themselves are unchanged and take props.
 */

export function AppStatusPage() {
  const { app, actor, actions, reloadAll } = useAppPageContext();
  return (
    <AppStatus app={app} actor={actor} actions={actions} onReload={reloadAll} />
  );
}

export function AppReleasesPage() {
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

export function AppSharingPage() {
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
