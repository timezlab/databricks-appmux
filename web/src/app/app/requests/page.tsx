import type { Metadata } from "next";

import { AppRequestsPage } from "@/components/app/app-pages";

export const metadata: Metadata = {
  title: "Requests",
  description:
    "Ask a platform admin to change one app's settings or delegation.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppRequestsPage />;
}
