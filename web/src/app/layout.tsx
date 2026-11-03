import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Notice } from "@/components/notice";
import { TopBar } from "@/components/top-bar";
import "./fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "appmux", template: "%s - appmux" },
  description: "Admin UI of appmux: launch, register and operate child apps.",
};

// "light dark" stays true: System, the default, renders either, and an
// explicit choice narrows `color-scheme` in globals.css.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
};

// `next.config.mjs` injects the build-time base path; the fallback serves the
// tests, which render this layout outside Next.
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "/_appmux";

/**
 * The document shell every page shares: the theme script, skip link, top bar,
 * one `<main>`. Pages render their content only and never their own `<main>`
 * (a second landmark would split the page for screen readers). The top bar and
 * the main landmark are server-rendered, so the page has structure before any
 * script runs; the pages themselves fill in after load.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // public/theme.js writes `data-theme` on <html> before React hydrates, so
    // the server HTML and the live element differ on that one attribute by
    // design (ADR 0019). suppressHydrationWarning covers this element's own
    // attributes only, not its children.
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* A blocking classic script (no async, defer or module), so the
            theme is on <html> before the first paint. A same-origin file, so
            the CSP's script-src 'self' allows it without a hash. */}
        <script src={`${BASE_PATH}/theme.js`} />
      </head>
      <body>
        {/* Parked above the viewport until it takes focus. */}
        <a
          className="absolute top-3 left-3 z-10 -translate-y-[200%] rounded-control bg-primary px-4 py-2 font-medium text-primary-foreground no-underline focus:translate-y-0"
          href="#main"
        >
          Skip to main content
        </a>
        <TopBar />
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
