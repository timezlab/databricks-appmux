"use client";

import { createContext, useContext } from "react";
import type { ReactNode } from "react";

import type { PlatformView, RequestList } from "@/lib/api";
import type { AdminViewer } from "@/lib/platform/admins";
import type { PollResult } from "@/lib/poll/schedule";

export interface PlatformContextValue {
  /** Fetches the platform view and the request list again, now (after a write). */
  refresh: () => void;
  /** Puts a sentence into the page's live region for screen readers. */
  announce: (message: string) => void;
}

const PlatformContext = createContext<PlatformContextValue | null>(null);

export function PlatformProvider({
  value,
  children,
}: {
  value: PlatformContextValue;
  children: ReactNode;
}) {
  return <PlatformContext value={value}>{children}</PlatformContext>;
}

/** What every section of the platform page shares; throws outside the page so a missing provider shows up at once. */
export function usePlatformContext(): PlatformContextValue {
  const value = useContext(PlatformContext);
  if (value === null) {
    throw new Error(
      "usePlatformContext must be used inside <PlatformProvider> (the platform page).",
    );
  }
  return value;
}

/** What the platform area has fetched, for the page on screen to read. */
export interface PlatformData {
  view: PlatformView;
  requests: PollResult<RequestList>;
  viewer: AdminViewer;
}

const PlatformDataContext = createContext<PlatformData | null>(null);

export function PlatformDataProvider({
  value,
  children,
}: {
  value: PlatformData;
  children: ReactNode;
}) {
  return <PlatformDataContext value={value}>{children}</PlatformDataContext>;
}

/** Throws outside the shell, like `usePlatformContext`. */
export function usePlatformData(): PlatformData {
  const value = useContext(PlatformDataContext);
  if (value === null) {
    throw new Error(
      "usePlatformData must be used inside <PlatformDataProvider> (the platform shell).",
    );
  }
  return value;
}
