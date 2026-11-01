import type { Metadata } from "next";

import { AppSharingPage } from "@/components/app/app-pages";

export const metadata: Metadata = {
  title: "Sharing",
  description: "Who may use or manage one app.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppSharingPage />;
}
