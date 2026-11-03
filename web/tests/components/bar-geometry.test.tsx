/**
 * One geometry for every bar (docs/product/admin-ui-design.md, Layout; the
 * owner's point 8: the underline of the top navigation and of the tabs
 * differed in style, place and size). The bar lies on the item's edge, so the
 * item's padding is the gap between the label and the bar: the padding and the
 * bar both live in the `bar-item` utility of globals.css, and a vertical list
 * (the sidebar) adapts them with `bar-axis-vertical` on its container. These
 * tests render the real consumers (the top navigation, the Environment page's
 * tab bar and the sidebar) and fail when an item drops `bar-item`, sets a
 * padding of its own, or underlines its text.
 */
import { render, screen, within } from "@testing-library/react";
import { InboxIcon, LayoutGridIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppSidebar } from "@/components/app-sidebar";
import { EnvironmentPanel } from "@/components/app/environment/environment-panel";
import { MainNav } from "@/components/main-nav";
import { readExample } from "../support/contract-examples";
import { jsonResponse, stubFetch } from "../support/fake-fetch";

vi.mock("next/navigation", () => ({
  usePathname: () => "/platform/",
}));

afterEach(() => {
  vi.restoreAllMocks();
});

const TAB_BAR_NAME = "What the app starts with";

async function renderTheBars(): Promise<void> {
  stubFetch(() => jsonResponse(200, readExample("AppEnvironment")));
  render(
    <>
      <MainNav />
      <EnvironmentPanel slug="sales" />
      <AppSidebar
        label="App sections"
        items={[
          { href: "/app/?slug=sales", label: "Overview", icon: LayoutGridIcon },
          {
            href: "/app/requests/?slug=sales",
            label: "Requests",
            icon: InboxIcon,
            count: 2,
            countDescription: "2 waiting",
          },
        ]}
        current="/app/?slug=sales"
        backHref="/"
        backLabel="All apps"
      />
    </>,
  );
  await screen.findByRole("tablist", { name: TAB_BAR_NAME });
}

/** The links of one navigation landmark that are its items (the sidebar's back link is not one). */
function navItems(navName: string): HTMLElement[] {
  const nav = screen.getByRole("navigation", { name: navName });
  return within(nav)
    .getAllByRole("link")
    .filter((link) => link.closest("li") !== null);
}

function tabItems(): HTMLElement[] {
  return within(
    screen.getByRole("tablist", { name: TAB_BAR_NAME }),
  ).getAllByRole("tab");
}

/** Every bar's items, by the bar's name, with the attribute that marks the chosen one and its text. */
function bars(): {
  name: string;
  items: HTMLElement[];
  chosenBy: "aria-current" | "aria-selected";
  chosenText: string;
}[] {
  return [
    {
      name: "Main",
      items: navItems("Main"),
      chosenBy: "aria-current",
      chosenText: "Platform",
    },
    {
      name: "tab bar",
      items: tabItems(),
      chosenBy: "aria-selected",
      chosenText: "Variables",
    },
    {
      name: "App sections",
      items: navItems("App sections"),
      chosenBy: "aria-current",
      chosenText: "Overview",
    },
  ];
}

function barClasses(element: HTMLElement): string[] {
  return [...element.classList].filter((name) => name.startsWith("bar-"));
}

/** A Tailwind padding class, with or without variants: `py-3`, `md:px-2`, `pb-1`, `p-0`. */
const PADDING_CLASS = /^(?:[\w-]+:)*-?p[xytblrse]?-/;

describe("the bar of the top navigation, the tab bar and the sidebar", () => {
  it("every item of each bar has the one bar class, and the sidebar's list its axis", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      expect(bar.items.length, bar.name).toBeGreaterThanOrEqual(2);
      for (const item of bar.items) {
        expect(barClasses(item), `${bar.name}: ${item.textContent}`).toEqual([
          "bar-item",
        ]);
      }
    }
    for (const item of navItems("App sections")) {
      expect(item.closest("ul")).toHaveClass("bar-axis-vertical");
    }
  });

  it("no item sets a padding of its own: the gap between label and bar is bar-item's", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      for (const item of bar.items) {
        expect(
          [...item.classList].filter((name) => PADDING_CLASS.test(name)),
          `${bar.name}: ${item.textContent}`,
        ).toEqual([]);
      }
    }
  });

  it("no item underlines its text, at rest or on hover", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      for (const item of bar.items) {
        expect(item.className).not.toMatch(/(^|\s|:)underline(\s|$)/);
        expect(item.className).not.toMatch(/border-b/);
      }
    }
  });

  it("the chosen item of each bar, and only it, is marked as chosen", async () => {
    await renderTheBars();
    for (const bar of bars()) {
      const chosen = bar.items.filter((item) =>
        bar.chosenBy === "aria-current"
          ? item.getAttribute("aria-current") === "page"
          : item.getAttribute("aria-selected") === "true",
      );
      expect(chosen, bar.name).toHaveLength(1);
      expect(chosen[0]).toHaveTextContent(bar.chosenText);
    }
  });
});
