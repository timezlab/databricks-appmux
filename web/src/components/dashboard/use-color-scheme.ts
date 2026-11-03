import { useSyncExternalStore } from "react";

export type ColorScheme = "light" | "dark";

const DARK_QUERY = "(prefers-color-scheme: dark)";

/**
 * The scheme on screen: the one the theme switch wrote on `<html>` as
 * `data-theme`, else the operating system's.
 */
function readColorScheme(): ColorScheme {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "light" || chosen === "dark") return chosen;
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  const media = window.matchMedia(DARK_QUERY);
  media.addEventListener("change", onChange);
  return () => {
    observer.disconnect();
    media.removeEventListener("change", onChange);
  };
}

function serverColorScheme(): ColorScheme {
  return "light";
}

/**
 * The colour scheme, kept current while the page is open: the theme switch
 * changing `data-theme` on `<html>`, or the operating system switching while
 * the page follows it. The charts paint with `var(--chart-N)`, which follows
 * the scheme by itself; they are keyed by this value so the SVG texture
 * patterns, which a browser may keep painted in the old colours, are drawn
 * again. "light" while the server prerenders and the browser hydrates.
 */
export function useColorScheme(): ColorScheme {
  return useSyncExternalStore(subscribe, readColorScheme, serverColorScheme);
}
