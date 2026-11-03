"use client";

import { SectionPlaceholder } from "../section-placeholder";

/** `/app/logs/`: the app's output. */
export function AppLogsPage() {
  return (
    <SectionPlaceholder
      title="Logs"
      description="The last lines of the app's output will show here, with the exit code and signal when it stopped."
    />
  );
}
