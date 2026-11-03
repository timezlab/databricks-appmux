"use client";

import { useAppPageContext } from "../app-context";
import { AppEgressSection } from "../security/app-egress-section";
import { ReachSection } from "../security/reach-section";
import { SandboxSection } from "../security/sandbox-section";

/**
 * `/app/security/`: the app's own egress rules (editable by a platform admin
 * only, design 6.3), where its connections may go in the order appmux decides,
 * and what its sandbox forbids, in words.
 */
export function AppSecurityPage() {
  const { app, actor, reloadAll } = useAppPageContext();
  return (
    <div className="flex flex-col gap-8">
      <AppEgressSection app={app} actor={actor} onReload={reloadAll} />
      <div className="border-t pt-8">
        <ReachSection app={app} actor={actor} />
      </div>
      <div className="border-t pt-8">
        <SandboxSection />
      </div>
    </div>
  );
}
