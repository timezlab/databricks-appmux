import type { Metadata } from "next";

import { AppResourcesPage } from "@/components/app/app-pages";

export const metadata: Metadata = {
  title: "Resources",
  description: "What one app needs and what is attached to it.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppResourcesPage />;
}
