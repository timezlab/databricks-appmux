"use client";

import type { ReactNode } from "react";

import { SETTING_GROUPS } from "@/lib/platform/sections";
import { JumpLinks } from "@/components/jump-links";
import { AdminsSection } from "./admins-section";
import { AppsSection } from "./apps-section";
import { DelegationsSection } from "./delegations-section";
import { GlobalEgressSection } from "./global-egress-section";
import { InboxSection } from "./inbox-section";
import { MemorySection } from "./memory-section";
import { usePlatformData } from "./platform-context";
import { RegisterSection } from "./register-section";
import { SelfCheckSection } from "./self-check-section";
import { SettingsGroups } from "./settings-section";

/*
 * The five pages of the platform area. Each reads what the shell fetched
 * (`PlatformPage`) and puts one concern on screen; the sections are unchanged
 * and take props.
 */

/** The sections of one area, ruled, not boxed (admin-ui-design.md, Layout): the panel is the only card. */
function RuledSections({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-8 *:not-first:border-t *:not-first:pt-8">
      {children}
    </div>
  );
}

/** What waits for an answer first, then the health of the slot. */
export function PlatformOverviewPage() {
  const { view, requests } = usePlatformData();
  return (
    <RuledSections>
      <InboxSection requests={requests} view={view} />
      <MemorySection view={view} />
      <SelfCheckSection view={view} />
    </RuledSections>
  );
}

/** The apps, the memory each one's budget takes in the slot, and registering one. */
export function PlatformAppsPage() {
  const { view, requests } = usePlatformData();
  return (
    <RuledSections>
      <AppsSection view={view} requests={requests} />
      <MemorySection view={view} />
      <RegisterSection view={view} />
    </RuledSections>
  );
}

export function PlatformAccessPage() {
  const { view, viewer } = usePlatformData();
  return (
    <RuledSections>
      <AdminsSection view={view} viewer={viewer} />
      <DelegationsSection view={view} />
    </RuledSections>
  );
}

export function PlatformEgressPage() {
  const { view } = usePlatformData();
  return <GlobalEgressSection view={view} />;
}

export function PlatformSettingsPage() {
  const { view } = usePlatformData();
  return (
    <div className="flex flex-col gap-6">
      <JumpLinks
        label="Setting groups"
        items={SETTING_GROUPS.map((group) => ({
          label: group.title,
          href: group.href,
        }))}
      />
      <SettingsGroups view={view} />
    </div>
  );
}
