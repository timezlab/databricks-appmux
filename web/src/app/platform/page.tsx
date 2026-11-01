import type { Metadata } from "next";

import { PlatformOverviewPage } from "@/components/platform/platform-pages";

export const metadata: Metadata = {
  title: "Platform",
  description:
    "Requests waiting for an answer, memory and the self-check of this appmux.",
};

// A thin shell: the layout above polls the admin API, so the page runs in the browser.
export default function Page() {
  return <PlatformOverviewPage />;
}
