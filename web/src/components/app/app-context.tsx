"use client";

import { createContext, useContext } from "react";
import type { ReactNode } from "react";

import type { AppDetail, RequestList } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import type { PollResult } from "@/lib/poll/schedule";
import type { AppActions } from "./use-app-actions";

/** What every page of one app shares: the app as last fetched, who is looking, and the one write queue. */
export interface AppPageContextValue {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  requests: PollResult<RequestList>;
  /** Reads the requests again (after a withdraw). */
  reloadRequests: () => void;
  /** Reads the app, the launcher list and the requests again: offered after a 409. */
  reloadAll: () => void;
}

const AppPageContext = createContext<AppPageContextValue | null>(null);

export function AppPageProvider({
  value,
  children,
}: {
  value: AppPageContextValue;
  children: ReactNode;
}) {
  return <AppPageContext value={value}>{children}</AppPageContext>;
}

/** Throws outside the shell, so a page mounted without its provider shows up at once. */
export function useAppPageContext(): AppPageContextValue {
  const value = useContext(AppPageContext);
  if (value === null) {
    throw new Error(
      "useAppPageContext must be used inside <AppView> (the shell of an app's pages).",
    );
  }
  return value;
}
