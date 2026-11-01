import { useSyncExternalStore } from "react";

function subscribeToNothing(): () => void {
  return () => undefined;
}

/**
 * `false` while the server prerenders and while the browser hydrates that
 * HTML, `true` for every render after (and for a render that never was
 * prerendered). Gate anything that must not be in the static export on it: a
 * chart measures the DOM and writes `style` attributes, which the CSP refuses
 * in prerendered HTML. `useSyncExternalStore` gives the server's `false` during
 * hydration without an effect that sets state, so there is no mismatch and no
 * extra render once mounted.
 */
export function useIsClient(): boolean {
  return useSyncExternalStore(
    subscribeToNothing,
    () => true,
    () => false,
  );
}
