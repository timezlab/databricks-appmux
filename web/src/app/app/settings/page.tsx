import type { Metadata } from "next";

import { AppSettingsPage } from "@/components/app/pages/settings-page";

export const metadata: Metadata = {
  title: "Settings",
  description:
    "The name, address, source folder, memory budget and owner of one app (this section is being built).",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppSettingsPage />;
}
