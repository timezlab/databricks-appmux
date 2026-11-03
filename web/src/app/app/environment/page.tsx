import type { Metadata } from "next";

import { AppEnvironmentPage } from "@/components/app/pages/environment-page";

export const metadata: Metadata = {
  title: "Environment",
  description: "The runtime, variables and packages one app starts with.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppEnvironmentPage />;
}
