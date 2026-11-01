import type { Metadata } from "next";

import { PlatformAccessPage } from "@/components/platform/platform-pages";

export const metadata: Metadata = {
  title: "Platform access",
  description: "Platform admins and the delegations granted to apps.",
};

// A thin shell: the layout above polls the admin API, so the page runs in the browser.
export default function Page() {
  return <PlatformAccessPage />;
}
