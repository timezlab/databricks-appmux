"use client";

import { SETTING_GROUPS } from "@/lib/platform/sections";
import { pollData } from "@/lib/poll/data";
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

/** What waits for an answer first, then the health of the slot. */
export function PlatformOverviewPage() {
  const { view, requests } = usePlatformData();
  return (
    <>
      <InboxSection requests={requests} view={view} />
      <MemorySection view={view} />
      <SelfCheckSection view={view} />
    </>
  );
}

/** The apps, the memory each one's budget takes in the slot, and registering one. */
export function PlatformAppsPage() {
  const { view, requests } = usePlatformData();
  return (
    <>
      <AppsSection view={view} requests={pollData(requests)?.items ?? null} />
      <MemorySection view={view} />
      <RegisterSection view={view} />
    </>
  );
}

export function PlatformAccessPage() {
  const { view, viewer } = usePlatformData();
  return (
    <>
      <AdminsSection view={view} viewer={viewer} />
      <DelegationsSection view={view} />
    </>
  );
}

export function PlatformEgressPage() {
  const { view } = usePlatformData();
  return <GlobalEgressSection view={view} />;
}

export function PlatformSettingsPage() {
  const { view } = usePlatformData();
  return (
    <div className="grid gap-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
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
