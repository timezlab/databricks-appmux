import type { Metadata, Viewport } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { MainNav } from "@/components/main-nav";
import { Notice } from "@/components/notice";
import "./fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "appmux", template: "%s - appmux" },
  description: "Admin UI of appmux: launch, register and operate child apps.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
};

/**
 * The document shell every page shares: skip link, top navigation, one
 * `<main>`. Pages render their content only and never their own `<main>`
 * (a second landmark would split the page for screen readers). The header and
 * the main landmark are server-rendered, so the page has structure before any
 * script runs; the pages themselves fill in after load.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Parked above the viewport until it takes focus. */}
        <a
          className="absolute top-3 left-3 z-10 -translate-y-[200%] rounded-control bg-primary px-4 py-2 font-medium text-primary-foreground no-underline focus:translate-y-0"
          href="#main"
        >
          Skip to main content
        </a>
        {/* The top bar is the page colour with one hairline under it; the wordmark is plain text, there is no logo. */}
        <header className="border-b bg-background">
          <div className="mx-auto flex max-w-6xl flex-wrap items-end gap-x-6 px-4">
            <Link
              className="flex items-baseline gap-2 py-3 text-xl font-semibold tracking-heading text-foreground no-underline hover:no-underline"
              href="/"
            >
              <span>appmux</span>
              <span className="hidden text-xs font-normal tracking-normal text-muted-foreground sm:inline">
                for Databricks Apps
              </span>
            </Link>
            <MainNav />
          </div>
        </header>
        {/* The page's top-level blocks (PageHeader, Sections, notices) sit in one column with one gap between them. */}
        <main
          id="main"
          className="mx-auto flex max-w-6xl flex-col gap-6 px-4 pt-8 pb-16 focus:outline-none"
          tabIndex={-1}
        >
          <noscript>
            <Notice tone="warning" title="JavaScript is turned off">
              <p>The appmux admin UI needs JavaScript to load its data.</p>
            </Notice>
          </noscript>
          {children}
        </main>
      </body>
    </html>
  );
}
