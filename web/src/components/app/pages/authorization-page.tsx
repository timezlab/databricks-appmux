"use client";

import { pollData } from "@/lib/app/view";
import { useAppPageContext } from "../app-context";
import { IdentitySection, PermissionsSection } from "../authorization-section";

/** `/app/authorization/`: who may use or manage the app, and the identity it reaches Databricks with. */
export function AppAuthorizationPage() {
  const { app, actor, actions, requests, reloadAll } = useAppPageContext();
  return (
    <>
      <PermissionsSection
        app={app}
        actor={actor}
        actions={actions}
        onReload={reloadAll}
      />
      <IdentitySection
        app={app}
        actor={actor}
        actions={actions}
        requests={pollData(requests)?.items ?? null}
        onReload={reloadAll}
      />
    </>
  );
}
