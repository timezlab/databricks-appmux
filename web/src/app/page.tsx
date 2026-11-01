import type { Metadata } from "next";

import { Launcher } from "@/components/launcher/launcher";

export const metadata: Metadata = { title: "Apps" };

// A thin shell: the launcher polls the admin API, so it runs in the browser.
export default function LauncherPage() {
  return <Launcher />;
}
