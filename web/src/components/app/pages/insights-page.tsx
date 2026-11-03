"use client";

import { SectionPlaceholder } from "../section-placeholder";

/** `/app/insights/`: the dashboard's charts for this app alone. */
export function AppInsightsPage() {
  return (
    <SectionPlaceholder
      title="Insights"
      description="The dashboard's charts for this app alone will show here; until then, choose the app on the Dashboard."
    />
  );
}
