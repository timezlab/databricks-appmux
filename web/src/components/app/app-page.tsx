"use client";

import { useSearchParams } from "next/navigation";
import type { ReactNode } from "react";

import { readAppParam } from "@/lib/app/params";
import { AppProblem } from "./app-problem";
import { AppView } from "./app-view";

/**
 * The shell of the app pages (design 10.1), the layout of `/app/*`. A static export has no page per slug, so the slug
 * is read from the query string (`/app/?slug=sales`) in the browser; what it
 * names is read from the API after mount. `key` gives each slug its own state,
 * so moving from one app to another never keeps the first one's messages or forms.
 */
export function AppPage({ children }: { children: ReactNode }) {
  const param = readAppParam(useSearchParams());
  switch (param.status) {
    case "valid":
      return (
        <AppView key={param.slug} slug={param.slug}>
          {children}
        </AppView>
      );
    case "missing":
      return <AppProblem kind="missing" />;
    case "invalid":
      return <AppProblem kind="invalid" value={param.value} />;
  }
}
