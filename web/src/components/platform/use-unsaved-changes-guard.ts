"use client";

import { useEffect, useState } from "react";

/** Where "Leave" goes. Replaced in tests: jsdom cannot navigate. */
function navigateTo(url: string): void {
  window.location.assign(url);
}

export interface UnsavedChangesGuard {
  /** The address of the link the person followed while there were unsaved changes, or `null`. */
  pendingUrl: string | null;
  /** Stay on the page: the link is not followed. */
  stay: () => void;
  /** Follow the link the person chose. The caller drops the changes first. */
  leave: () => void;
}

function isPlainLeftClick(event: MouseEvent): boolean {
  return (
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

/** The address of the link a click lands on if following it would leave this page; otherwise `null`. */
function leavingUrl(event: MouseEvent): string | null {
  const target = event.target;
  if (!(target instanceof Element)) return null;
  const link = target.closest("a[href]");
  if (!(link instanceof HTMLAnchorElement)) return null;
  // A new tab or a download keeps this page; a hash on this page keeps it too.
  if (link.target === "_blank" || link.hasAttribute("download")) return null;
  const url = new URL(link.href, window.location.href);
  const here = window.location;
  if (
    url.origin === here.origin &&
    url.pathname === here.pathname &&
    url.search === here.search
  ) {
    return null;
  }
  return url.href;
}

/**
 * Keeps unsaved edits from vanishing without a word while `isDirty`.
 *
 * Closing or reloading the tab asks the browser to confirm (`beforeunload`;
 * the browser's own wording, which no page can change). Following a link of the
 * page (a tab of the navigation, the header) is held back and handed to the
 * caller as `pendingUrl`, so the page can ask in its own words and offer "Stay"
 * and "Leave". The tab bar is a client-side navigation, which has no event to
 * cancel: the click is the only place to stop it.
 */
export function useUnsavedChangesGuard(
  isDirty: boolean,
  navigate: (url: string) => void = navigateTo,
): UnsavedChangesGuard {
  const [pendingUrl, setPendingUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!isDirty) return;
    function handleBeforeUnload(event: BeforeUnloadEvent): void {
      event.preventDefault();
      // Some browsers show their prompt only when the property is set.
      event.returnValue = "";
    }
    function handleClick(event: MouseEvent): void {
      if (event.defaultPrevented || !isPlainLeftClick(event)) return;
      const url = leavingUrl(event);
      if (url === null) return;
      event.preventDefault();
      // Before React's own listener at the root, so a `next/link` does not navigate.
      event.stopPropagation();
      setPendingUrl(url);
    }
    window.addEventListener("beforeunload", handleBeforeUnload);
    document.addEventListener("click", handleClick, true);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      document.removeEventListener("click", handleClick, true);
    };
  }, [isDirty]);

  return {
    // Nothing to ask about once the changes are gone.
    pendingUrl: isDirty ? pendingUrl : null,
    stay: () => {
      setPendingUrl(null);
    },
    leave: () => {
      if (pendingUrl !== null) navigate(pendingUrl);
    },
  };
}
