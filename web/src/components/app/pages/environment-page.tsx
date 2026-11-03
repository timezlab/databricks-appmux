"use client";

import { useAppPageContext } from "../app-context";
import { EnvironmentPanel } from "../environment/environment-panel";

/** `/app/environment/`: the runtime, variables and packages the app starts with. */
export function AppEnvironmentPage() {
  const { app } = useAppPageContext();
  return <EnvironmentPanel slug={app.slug} />;
}
