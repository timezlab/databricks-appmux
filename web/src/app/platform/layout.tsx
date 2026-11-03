import type { ReactNode } from "react";

import { PlatformPage } from "@/components/platform/platform-page";

/**
 * The layout of every platform page: the access check, the polls and the
 * sidebar of the five areas (see `PlatformPage`). It stays mounted while the
 * admin moves between areas, so nothing is fetched again for a move.
 */
export default function PlatformLayout({ children }: { children: ReactNode }) {
  return <PlatformPage>{children}</PlatformPage>;
}
