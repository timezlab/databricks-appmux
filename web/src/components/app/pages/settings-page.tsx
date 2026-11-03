"use client";

import { SectionPlaceholder } from "../section-placeholder";

/**
 * `/app/settings/`: the app's platform fields. The Status page had no form for
 * them (it showed the source folder and the memory budget as facts, which stay
 * on Overview as the design wants), and the way to ask for a change is on
 * Requests, so nothing moved here yet.
 */
export function AppSettingsPage() {
  return (
    <SectionPlaceholder
      title="Settings"
      description="The app's name, address, source folder, memory budget and owner will be edited here. Until then, ask a platform admin for a change under Requests."
    />
  );
}
