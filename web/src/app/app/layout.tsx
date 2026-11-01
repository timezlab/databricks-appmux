import type { ReactNode } from "react";
import { Suspense } from "react";

import { AppPage } from "@/components/app/app-page";
import { Loading } from "@/components/loading";

/**
 * The layout of every page of one app. The slug is in the query string
 * (`/app/?slug=sales`), which a static export can read only in the browser,
 * so the client shell waits inside `Suspense`: the prerendered HTML holds the
 * header and "Loading", and the data is read after the page mounts. The shell
 * stays mounted while the person moves between the app's pages, so the polls
 * keep their data and a half-typed form is not lost to a tab change.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<Loading label="Loading the app" />}>
      <AppPage>{children}</AppPage>
    </Suspense>
  );
}
