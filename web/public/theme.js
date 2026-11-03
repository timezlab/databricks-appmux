/*
 * The admin UI's theme choice: Light, Dark or System (docs/decisions/0019).
 *
 * The layout loads this file as a blocking classic script in <head>, so it
 * runs before the first paint: it reads the stored choice and writes it as
 * `data-theme` on <html>, and the page never flashes the wrong theme. A file
 * and not an inline script, because the CSP (script-src 'self' plus hashes)
 * allows a same-origin file as it is.
 *
 * It never resolves System itself. globals.css does, with
 * prefers-color-scheme: System keeps following the OS while the page is open,
 * and a page whose script was blocked gets the OS theme all the same.
 *
 * This file is the one copy of the rule. The React toggle calls the
 * `window.appmuxTheme` it exposes (src/hooks/use-theme-choice.ts), and
 * tests/unit/theme-script.test.ts runs this file in jsdom.
 */
(() => {
  "use strict";

  const STORAGE_KEY = "appmux.theme";
  const CHOICES = Object.freeze(["light", "dark", "system"]);
  const DEFAULT_CHOICE = "system";
  const root = document.documentElement;
  const listeners = new Set();

  /** A stored value as a choice; anything else (a typo, an old value) is System. */
  const parse = (value) => (CHOICES.includes(value) ? value : DEFAULT_CHOICE);

  // Reaching localStorage can throw by itself (blocked site data), and so can
  // every read and write: the page works without it.
  const readStored = () => {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch {
      return null; // storage is blocked: the default stands in for it
    }
  };

  const writeStored = (choice) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, choice);
    } catch {
      // Blocked or full: the choice still holds for this page.
    }
  };

  let current = parse(readStored());

  const apply = (choice) => {
    current = choice;
    root.setAttribute("data-theme", choice);
    for (const listener of listeners) listener();
  };

  const choose = (choice) => {
    if (!CHOICES.includes(choice)) {
      throw new TypeError(`Unknown theme choice: ${String(choice)}`);
    }
    writeStored(choice);
    apply(choice);
  };

  const subscribe = (listener) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  };

  // A choice made in another tab of this site; a null key is a clear().
  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY || event.key === null) {
      apply(parse(event.key === null ? null : event.newValue));
    }
  });

  root.setAttribute("data-theme", current);

  window.appmuxTheme = Object.freeze({
    storageKey: STORAGE_KEY,
    choices: CHOICES,
    parse,
    current: () => current,
    choose,
    subscribe,
  });
})();
