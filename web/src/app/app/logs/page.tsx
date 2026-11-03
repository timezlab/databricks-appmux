import type { Metadata } from "next";

import { AppLogsPage } from "@/components/app/pages/logs-page";

export const metadata: Metadata = {
  title: "Logs",
  description: "The output of one app (this section is being built).",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppLogsPage />;
}
