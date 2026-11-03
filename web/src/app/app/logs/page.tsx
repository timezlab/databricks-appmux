import type { Metadata } from "next";

import { AppLogsPage } from "@/components/app/pages/logs-page";

export const metadata: Metadata = {
  title: "Logs",
  description: "The last lines of one app's output, as they arrive.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppLogsPage />;
}
