import { useSyncExternalStore } from "react";

/*
 * The display names of apps this page has already read, so the top bar's
 * breadcrumb can say "Sales dashboard" without a request of its own. Whatever
 * reads an app (the switcher's list, the app page) hands the names over; until
 * one has, the breadcrumb shows the app's route strip. In memory only: a
 * reload starts empty, and the API stays the source.
 */
const displayNames = new Map<string, string>();
const listeners = new Set<() => void>();

/** Keeps the names of these apps for the breadcrumb. */
export function rememberAppDisplayNames(
  apps: readonly { slug: string; display_name: string }[],
): void {
  let isChanged = false;
  for (const app of apps) {
    if (displayNames.get(app.slug) !== app.display_name) {
      displayNames.set(app.slug, app.display_name);
      isChanged = true;
    }
  }
  if (!isChanged) return;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The display name of `slug` if this page has read it, else `undefined`. */
export function useAppDisplayName(slug: string): string | undefined {
  return useSyncExternalStore(
    subscribe,
    () => displayNames.get(slug),
    () => undefined,
  );
}
