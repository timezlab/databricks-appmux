import type { Metadata } from "next";

import { PlatformAppsPage } from "@/components/platform/platform-pages";

export const metadata: Metadata = {
  title: "Platform apps",
  description: "Every app's platform fields, and registering a new one.",
};

// A thin shell: the layout above polls the admin API, so the page runs in the browser.
export default function Page() {
  return <PlatformAppsPage />;
}
