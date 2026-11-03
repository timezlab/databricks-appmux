import { afterAll, beforeAll } from "vitest";

/**
 * jsdom has no `window.matchMedia`, which a browser always has and the charts'
 * colour-scheme hook reads. Call `fakeMatchMedia()` at the top of a test file
 * that draws a chart: every query answers "does not match", except that the
 * returned `prefersDark` can be switched, firing `change` as a browser does
 * when the operating system changes its scheme.
 */
export const DARK_QUERY = "(prefers-color-scheme: dark)";

class FakeMediaQueryList extends EventTarget implements MediaQueryList {
  readonly media: string;
  matches: boolean;
  onchange: MediaQueryList["onchange"] = null;

  constructor(media: string, matches: boolean) {
    super();
    this.media = media;
    this.matches = matches;
  }

  /** Deprecated in browsers; nothing here uses it. */
  addListener(): void {
    throw new Error("FakeMediaQueryList: use addEventListener");
  }

  removeListener(): void {
    throw new Error("FakeMediaQueryList: use removeEventListener");
  }

  setMatches(matches: boolean): void {
    this.matches = matches;
    this.dispatchEvent(new Event("change"));
  }
}

export interface FakeMatchMedia {
  /** Switches the operating system's scheme and tells the listeners. */
  setPrefersDark: (isDark: boolean) => void;
}

export function fakeMatchMedia(): FakeMatchMedia {
  let original: PropertyDescriptor | undefined;
  const dark = new FakeMediaQueryList(DARK_QUERY, false);

  beforeAll(() => {
    original = Object.getOwnPropertyDescriptor(window, "matchMedia");
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: (query: string): MediaQueryList =>
        query === DARK_QUERY ? dark : new FakeMediaQueryList(query, false),
    });
  });

  afterAll(() => {
    if (original === undefined) {
      Reflect.deleteProperty(window, "matchMedia");
    } else {
      Object.defineProperty(window, "matchMedia", original);
    }
  });

  return {
    setPrefersDark: (isDark) => {
      dark.setMatches(isDark);
    },
  };
}
