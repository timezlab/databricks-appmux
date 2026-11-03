import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import path from "node:path";
import { runInThisContext } from "node:vm";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ThemeToggle } from "@/components/theme-toggle";
import { THEME_STORAGE_KEY } from "@/hooks/use-theme-choice";

const SCRIPT_PATH = path.resolve(import.meta.dirname, "../../public/theme.js");

/** What the head of the page does before React: run public/theme.js. */
function runThemeScript(): void {
  runInThisContext(readFileSync(SCRIPT_PATH, "utf8"), {
    filename: SCRIPT_PATH,
  });
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  delete window.appmuxTheme;
});

afterEach(() => {
  window.localStorage.clear();
});

describe("ThemeToggle", () => {
  it("names the current choice, and System is the default", () => {
    runThemeScript();
    render(<ThemeToggle />);
    expect(
      screen.getByRole("button", { name: "Theme: System" }),
    ).toBeInTheDocument();
  });

  it("reads a stored choice", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    runThemeScript();
    render(<ThemeToggle />);
    expect(
      screen.getByRole("button", { name: "Theme: Dark" }),
    ).toBeInTheDocument();
  });

  it("offers Light, Dark and System with the current one checked, and applies a choice", async () => {
    const user = userEvent.setup();
    runThemeScript();
    render(<ThemeToggle />);

    await user.click(screen.getByRole("button", { name: "Theme: System" }));
    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((item) => item.textContent)).toEqual([
      "Light",
      "Dark",
      "System",
    ]);
    expect(
      screen.getByRole("menuitemradio", { name: "System" }),
    ).toHaveAttribute("aria-checked", "true");

    await user.click(screen.getByRole("menuitemradio", { name: "Dark" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Theme: Dark" }),
    ).toBeInTheDocument();
  });

  it("follows a choice made in another tab", () => {
    runThemeScript();
    render(<ThemeToggle />);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: THEME_STORAGE_KEY,
          newValue: "light",
        }),
      );
    });
    expect(
      screen.getByRole("button", { name: "Theme: Light" }),
    ).toBeInTheDocument();
  });

  it("says the switch did not load, and offers nothing, when the theme script never ran", () => {
    render(<ThemeToggle />);
    const button = screen.getByRole("button", {
      name: "Theme: System (the theme switch did not load)",
    });
    expect(button).toBeDisabled();
  });

  it("renders on the server with no style attribute and the default choice", () => {
    const html = renderToStaticMarkup(<ThemeToggle />);
    expect(html).not.toMatch(/\sstyle=/);
    expect(html).toContain('aria-label="Theme: System"');
  });
});
