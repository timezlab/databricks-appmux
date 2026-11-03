import type { Metadata } from "next";

import { AppInsightsPage } from "@/components/app/pages/insights-page";

export const metadata: Metadata = {
  title: "Insights",
  description: "The metrics of one app (this section is being built).",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppInsightsPage />;
}
