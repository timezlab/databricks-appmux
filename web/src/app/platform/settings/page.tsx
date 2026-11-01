import type { Metadata } from "next";

import { PlatformSettingsPage } from "@/components/platform/platform-pages";

export const metadata: Metadata = {
  title: "Platform settings",
  description: "Limits and switches that reach every app.",
};

// A thin shell: the layout above polls the admin API, so the page runs in the browser.
export default function Page() {
  return <PlatformSettingsPage />;
}
