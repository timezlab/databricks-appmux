import type { Metadata } from "next";

import { AppSecurityPage } from "@/components/app/pages/security-page";

export const metadata: Metadata = {
  title: "Security",
  description:
    "The egress rules and sandbox of one app (this section is being built).",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppSecurityPage />;
}
