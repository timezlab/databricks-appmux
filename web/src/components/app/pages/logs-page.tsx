"use client";

import { useAppPageContext } from "../app-context";
import { LogsPanel } from "../logs/logs-panel";

/** `/app/logs/`: the app's output, polled while the tab is visible. */
export function AppLogsPage() {
  const { app } = useAppPageContext();
  return <LogsPanel slug={app.slug} appName={app.display_name} />;
}
