import type { Metadata } from "next";

import { AppStatusPage } from "@/components/app/app-pages";

export const metadata: Metadata = {
  title: "Status",
  description:
    "The status of one app: its state, what needs attention, and update, start, stop and restart.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppStatusPage />;
}
