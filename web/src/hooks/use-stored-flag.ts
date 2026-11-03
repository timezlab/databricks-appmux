import { useSyncExternalStore } from "react";

/**
 * Values written while `localStorage` refused them (a private window, blocked
 * site data): they last until the page is reloaded instead of being lost.
 */
const unsavedFlags = new Map<string, boolean>();
const listeners = new Set<() => void>();

function readFlag(key: string): boolean {
  try {
    const stored = window.localStorage.getItem(key);
    if (stored !== null) return stored === "true";
  } catch {
    // Storage is blocked: the in-memory value below stands in for it.
  }
  return unsavedFlags.get(key) ?? false;
}

function writeFlag(key: string, value: boolean): void {
  unsavedFlags.set(key, value);
  try {
    window.localStorage.setItem(key, String(value));
  } catch {
    // Storage is blocked or full: the in-memory value keeps it for this page.
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("storage", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

/**
 * A yes/no preference of this browser (a collapsed sidebar), kept in
 * `localStorage` under `key`. `false` while the page is prerendered and
 * hydrated, so the static HTML and the first client render agree; the stored
 * value follows right after. Every read and write is guarded: blocked storage
 * keeps the value for the life of the page and never throws.
 */
export function useStoredFlag(
  key: string,
): [value: boolean, setValue: (value: boolean) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => readFlag(key),
    () => false,
  );
  return [
    value,
    (next) => {
      writeFlag(key, next);
    },
  ];
}
