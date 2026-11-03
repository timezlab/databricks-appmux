import { useSyncExternalStore } from "react";

/**
 * The theme choices, in menu order. The rule that reads, stores and applies a
 * choice lives in one place, `public/theme.js` (ADR 0019): it runs in the head
 * before the first paint and exposes `window.appmuxTheme`, which this hook only
 * calls. tests/unit/theme-script.test.ts runs that file and checks these two
 * constants against it.
 */
export const THEME_CHOICES = ["light", "dark", "system"] as const;
export type ThemeChoice = (typeof THEME_CHOICES)[number];

/** Where the script keeps the choice; changing it forgets every stored choice. */
export const THEME_STORAGE_KEY = "appmux.theme";

/** What `public/theme.js` puts on `window`. */
export interface ThemeApi {
  readonly storageKey: string;
  readonly choices: readonly ThemeChoice[];
  /** A stored value as a choice; anything it does not know is System. */
  parse(value: string | null): ThemeChoice;
  current(): ThemeChoice;
  /** Applies the choice to `<html>` and stores it; throws a TypeError on an unknown one. */
  choose(choice: ThemeChoice): void;
  subscribe(listener: () => void): () => void;
}

declare global {
  interface Window {
    appmuxTheme?: ThemeApi;
  }
}

function subscribe(listener: () => void): () => void {
  return window.appmuxTheme?.subscribe(listener) ?? (() => undefined);
}

function currentChoice(): ThemeChoice | null {
  return window.appmuxTheme?.current() ?? null;
}

// The prerendered page cannot know the choice; System is the default.
function prerenderedChoice(): ThemeChoice {
  return "system";
}

/**
 * The theme choice of this browser and a way to change it. `choice` is `null`
 * when the theme script did not run (blocked by an extension or a proxy): the
 * page then follows the OS through CSS alone and there is nothing to change.
 */
export function useThemeChoice(): {
  choice: ThemeChoice | null;
  choose: (choice: ThemeChoice) => void;
} {
  const choice = useSyncExternalStore(
    subscribe,
    currentChoice,
    prerenderedChoice,
  );
  return {
    choice,
    choose: (next) => {
      window.appmuxTheme?.choose(next);
    },
  };
}
