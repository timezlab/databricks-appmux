import type { Metadata } from "next";

import { AppOverviewPage } from "@/components/app/pages/overview-page";

export const metadata: Metadata = {
  title: "Overview",
  description:
    "One app at a glance: its state, what needs attention, and update, start, stop and restart.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppOverviewPage />;
}
