// @vitest-environment jsdom
// The theme script is plain JavaScript the browser runs in the page's head; the
// test runs the same file in jsdom, so the rule has one source (ADR 0019).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { runInThisContext } from "node:vm";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import { THEME_CHOICES, THEME_STORAGE_KEY } from "@/hooks/use-theme-choice";

const SCRIPT_PATH = path.resolve(import.meta.dirname, "../../public/theme.js");
const source = readFileSync(SCRIPT_PATH, "utf8");

/** Runs public/theme.js as the head of a fresh page would, and returns what it exposes. */
function loadScript() {
  runInThisContext(source, { filename: SCRIPT_PATH });
  const api = window.appmuxTheme;
  assert.ok(api, "the script exposes window.appmuxTheme");
  return api;
}

function appliedTheme(): string | null {
  return document.documentElement.getAttribute("data-theme");
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  delete window.appmuxTheme;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("public/theme.js, reading the stored choice", () => {
  it("applies System when nothing is stored", () => {
    const api = loadScript();
    assert.equal(appliedTheme(), "system");
    assert.equal(api.current(), "system");
  });

  for (const choice of ["light", "dark", "system"] as const) {
    it(`applies a stored ${choice}`, () => {
      window.localStorage.setItem(THEME_STORAGE_KEY, choice);
      const api = loadScript();
      assert.equal(appliedTheme(), choice);
      assert.equal(api.current(), choice);
    });
  }

  it("ignores a stored value it does not know, and falls back to System", () => {
    for (const unknown of ["purple", "", "DARK", " dark", "null"]) {
      window.localStorage.setItem(THEME_STORAGE_KEY, unknown);
      const api = loadScript();
      assert.equal(appliedTheme(), "system", JSON.stringify(unknown));
      assert.equal(api.current(), "system", JSON.stringify(unknown));
    }
  });

  it("falls back to System, without throwing, when storage refuses the read", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const api = loadScript();
    assert.equal(appliedTheme(), "system");
    assert.equal(api.current(), "system");
  });

  it("falls back to System when even reaching localStorage throws (blocked site data)", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const api = loadScript();
    assert.equal(appliedTheme(), "system");
    api.choose("dark");
    assert.equal(appliedTheme(), "dark");
  });
});

describe("public/theme.js, choosing", () => {
  it("applies the choice, keeps it in localStorage and tells each subscriber", () => {
    const api = loadScript();
    const listener = vi.fn();
    const unsubscribe = api.subscribe(listener);

    api.choose("dark");
    assert.equal(appliedTheme(), "dark");
    assert.equal(api.current(), "dark");
    assert.equal(window.localStorage.getItem(THEME_STORAGE_KEY), "dark");
    assert.equal(listener.mock.calls.length, 1);

    unsubscribe();
    api.choose("light");
    assert.equal(appliedTheme(), "light");
    assert.equal(listener.mock.calls.length, 1);
  });

  it("still applies the choice for this page when storage refuses the write", () => {
    const api = loadScript();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    api.choose("light");
    assert.equal(appliedTheme(), "light");
    assert.equal(api.current(), "light");
  });

  it("refuses a choice it does not know and changes nothing", () => {
    const api = loadScript();
    api.choose("dark");
    assert.throws(() => {
      // @ts-expect-error -- the point is a value outside the type
      api.choose("purple");
    }, TypeError);
    assert.equal(appliedTheme(), "dark");
    assert.equal(window.localStorage.getItem(THEME_STORAGE_KEY), "dark");
  });
});

describe("public/theme.js, another tab", () => {
  function storageEvent(key: string | null, newValue: string | null) {
    window.dispatchEvent(
      new StorageEvent("storage", {
        key,
        newValue,
        storageArea: window.localStorage,
      }),
    );
  }

  it("follows a choice made in another tab", () => {
    const api = loadScript();
    const listener = vi.fn();
    api.subscribe(listener);

    storageEvent(THEME_STORAGE_KEY, "dark");
    assert.equal(appliedTheme(), "dark");
    assert.equal(api.current(), "dark");
    assert.equal(listener.mock.calls.length, 1);
  });

  it("goes back to System when another tab clears the choice or stores one it does not know", () => {
    const api = loadScript();
    api.choose("dark");
    storageEvent(THEME_STORAGE_KEY, null);
    assert.equal(appliedTheme(), "system");

    api.choose("dark");
    storageEvent(null, null); // localStorage.clear() in another tab
    assert.equal(appliedTheme(), "system");

    api.choose("dark");
    storageEvent(THEME_STORAGE_KEY, "purple");
    assert.equal(appliedTheme(), "system");
  });

  it("ignores a change to another key", () => {
    const api = loadScript();
    api.choose("dark");
    storageEvent("appmux.sidebar.collapsed", "true");
    assert.equal(appliedTheme(), "dark");
  });
});

describe("public/theme.js and the React side agree", () => {
  it("on the storage key and the choices", () => {
    const api = loadScript();
    assert.equal(api.storageKey, THEME_STORAGE_KEY);
    assert.deepEqual([...api.choices], [...THEME_CHOICES]);
  });

  it("parse is the one rule for a stored value", () => {
    const api = loadScript();
    assert.equal(api.parse("light"), "light");
    assert.equal(api.parse("dark"), "dark");
    assert.equal(api.parse("system"), "system");
    assert.equal(api.parse(null), "system");
    assert.equal(api.parse("Dark"), "system");
    assert.equal(api.parse("toString"), "system");
  });
});
