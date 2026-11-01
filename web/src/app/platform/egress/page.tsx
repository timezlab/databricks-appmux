import type { Metadata } from "next";

import { PlatformEgressPage } from "@/components/platform/platform-pages";

export const metadata: Metadata = {
  title: "Platform egress",
  description: "The egress rules every app is checked against.",
};

// A thin shell: the layout above polls the admin API, so the page runs in the browser.
export default function Page() {
  return <PlatformEgressPage />;
}
