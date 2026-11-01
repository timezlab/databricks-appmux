import type { Metadata } from "next";

import { DashboardView } from "@/components/dashboard/dashboard-view";

export const metadata: Metadata = { title: "Dashboard" };

/** A thin server page: the data is read in the browser, so the static export holds only the header and "Loading". */
export default function DashboardPage() {
  return <DashboardView />;
}
