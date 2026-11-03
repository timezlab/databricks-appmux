import type { Metadata } from "next";

import { AppAuthorizationPage } from "@/components/app/pages/authorization-page";

export const metadata: Metadata = {
  title: "Authorization",
  description: "Who may use or manage one app.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppAuthorizationPage />;
}
