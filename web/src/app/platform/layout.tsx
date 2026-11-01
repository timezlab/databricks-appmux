import type { ReactNode } from "react";

import { PlatformPage } from "@/components/platform/platform-page";

/**
 * The layout of every platform page: the access check, the polls and the tab
 * bar (see `PlatformPage`). It stays mounted while the admin moves between
 * tabs, so nothing is fetched again for a tab change.
 */
export default function PlatformLayout({ children }: { children: ReactNode }) {
  return <PlatformPage>{children}</PlatformPage>;
}
