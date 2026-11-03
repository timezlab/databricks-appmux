import type { Metadata } from "next";

import { AppDeploymentsPage } from "@/components/app/pages/deployments-page";

export const metadata: Metadata = {
  title: "Deployments",
  description: "The releases of one app: add, activate, edit, lock and delete.",
};

// A thin shell: the layout above reads the slug and the app, this page puts one area of it on screen.
export default function Page() {
  return <AppDeploymentsPage />;
}
